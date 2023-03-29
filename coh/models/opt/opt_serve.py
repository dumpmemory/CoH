import dataclasses
import os
import pprint
import re
from functools import partial
from threading import Lock

import absl
import flax
import jax
import jax.numpy as jnp
import numpy as np
import optax
from flask import Flask, request
from flax import linen as nn
from flax.jax_utils import prefetch_to_device
from flax.training.train_state import TrainState
from jax.experimental import PartitionSpec as PS
from jax.experimental.pjit import pjit, with_sharding_constraint
from tqdm import tqdm, trange

from coh.jax_utils import (
    JaxRNG,
    ShardingHelper,
    StreamingCheckpointer,
    cross_entropy_loss_and_accuracy,
    get_float_dtype_by_name,
    get_jax_mp_mesh,
    global_norm,
    match_partition_rules,
    named_tree_map,
    next_rng,
    set_random_seed,
)
from coh.models.opt.opt_model import (
    FlaxOPTForCausalLM,
    FlaxOPTForCausalLMModule,
    OPTConfig,
)
from coh.serving import LMServer
from coh.utils import (
    WandBLogger,
    define_flags_with_default,
    get_user_flags,
    load_pickle,
    user_flags_to_config_dict,
)

FLAGS_DEF = define_flags_with_default(
    seed=42,
    initialize_jax_distributed=False,
    mp_mesh_dim=1,
    dtype="bf16",
    input_length=1024,
    seq_length=2048,
    top_k=50,
    top_p=1.0,
    do_sample=True,
    temperature=1.0,
    num_beams=1,
    loglikelihood_add_bos_token=False,
    load_opt_config="",
    load_checkpoint="",
    tokenizer=OPTConfig.get_tokenizer_config(),
    lm_server=LMServer.get_default_config(),
)
FLAGS = absl.flags.FLAGS


def main(argv):
    if FLAGS.initialize_jax_distributed:
        jax.distributed.initialize()
    set_random_seed(FLAGS.seed)

    prefix_tokenizer = OPTConfig.get_tokenizer(
        FLAGS.tokenizer, truncation_side="left", padding_side="left"
    )
    tokenizer = OPTConfig.get_tokenizer(
        FLAGS.tokenizer, truncation_side="right", padding_side="right"
    )

    with jax.default_device(jax.devices("cpu")[0]):
        opt_config = OPTConfig.load_config(FLAGS.load_opt_config)
        load_type, load_path = FLAGS.load_checkpoint.split("::", 1)
        dtype = get_float_dtype_by_name(FLAGS.dtype)
        if load_type == "trainstate_params":
            params = flax.core.frozen_dict.freeze(
                StreamingCheckpointer.load_checkpoint(load_path, dtype=dtype)["params"]
            )
        elif load_type == "flax_params":
            params = flax.core.frozen_dict.freeze(
                {
                    "params": StreamingCheckpointer.load_flax_checkpoint(
                        load_path, dtype=dtype
                    )
                }
            )
        elif load_type == "huggingface":
            params = opt_config.load_pretrained(load_path, dtype=dtype)
        else:
            raise ValueError(f"Unsupported load checkpoint type {load_type}")

        hf_model = FlaxOPTForCausalLM(
            opt_config,
            input_shape=(1, FLAGS.seq_length),
            seed=FLAGS.seed,
            _do_init=False,
        )

    model_ps = match_partition_rules(OPTConfig.get_partition_rules(), params)
    sharding_helper = ShardingHelper(model_ps)

    @partial(
        pjit,
        in_axis_resources=(model_ps, PS(), PS()),
        out_axis_resources=(PS(), PS(), PS()),
    )
    def forward_loglikelihood(params, rng, batch):
        batch = with_sharding_constraint(batch, PS("dp"))
        rng_generator = JaxRNG(rng)
        input_tokens = batch["input_tokens"]
        output_tokens = batch["output_tokens"]
        input_mask = batch["input_mask"]
        output_mask = batch["output_mask"]

        logits = hf_model.module.apply(
            params,
            input_tokens,
            attention_mask=input_mask,
            deterministic=True,
            rngs=rng_generator(opt_config.rng_keys()),
        ).logits
        # if opt_config.n_real_tokens is not None:
        #     logits = logits.at[:, :, opt_config.n_real_tokens :].set(-1e8)
        loglikelihood = -optax.softmax_cross_entropy_with_integer_labels(
            logits, output_tokens
        )
        loglikelihood = jnp.sum(loglikelihood * output_mask, axis=-1)
        match_count = jnp.sum(
            (jnp.argmax(logits, axis=-1) == output_tokens) * output_mask, axis=-1
        )
        total = jnp.sum(output_mask, axis=-1)
        is_greedy = match_count == total
        return loglikelihood, is_greedy, rng_generator()

    @partial(
        pjit, in_axis_resources=(model_ps, PS(), PS()), out_axis_resources=(PS(), PS())
    )
    def forward_generate(params, rng, batch):
        batch = with_sharding_constraint(batch, PS("dp"))
        rng_generator = JaxRNG(rng)
        output = hf_model.generate(
            batch["input_tokens"],
            attention_mask=batch["attention_mask"],
            params=params["params"],
            prng_key=rng_generator(),
            max_length=FLAGS.seq_length,
            pad_token_id=tokenizer.eos_token_id,
            bos_token_id=tokenizer.bos_token_id,
            eos_token_id=tokenizer.eos_token_id,
            temperature=FLAGS.temperature,
            top_k=FLAGS.top_k,
            top_p=FLAGS.top_p,
            num_beams=FLAGS.num_beams,
            do_sample=FLAGS.do_sample,
        ).sequences[:, batch["input_tokens"].shape[1] :]
        return output, rng_generator()

    @partial(
        pjit, in_axis_resources=(model_ps, PS(), PS()), out_axis_resources=(PS(), PS())
    )
    def forward_greedy_generate(params, rng, batch):
        batch = with_sharding_constraint(batch, PS("dp"))
        rng_generator = JaxRNG(rng)
        output = hf_model.generate(
            batch["input_tokens"],
            attention_mask=batch["attention_mask"],
            params=params["params"],
            prng_key=rng_generator(),
            max_length=FLAGS.seq_length,
            pad_token_id=tokenizer.eos_token_id,
            bos_token_id=tokenizer.bos_token_id,
            eos_token_id=tokenizer.eos_token_id,
            num_beams=1,
            do_sample=False,
        ).sequences[:, batch["input_tokens"].shape[1] :]
        return output, rng_generator()

    mesh = get_jax_mp_mesh(FLAGS.mp_mesh_dim)
    with mesh:
        params = sharding_helper.put(params)
        sharded_rng = next_rng()

    class OPTServer(LMServer):
        @staticmethod
        def loglikelihood(prefix_text, text):
            nonlocal sharded_rng
            prefix = prefix_tokenizer(
                prefix_text,
                padding="max_length",
                truncation=True,
                max_length=FLAGS.input_length,
                return_tensors="np",
            )
            inputs = tokenizer(
                text,
                padding="max_length",
                truncation=True,
                max_length=FLAGS.seq_length - FLAGS.input_length,
                return_tensors="np",
            )
            output_tokens = np.concatenate([prefix.input_ids, inputs.input_ids], axis=1)
            bos_tokens = np.full(
                (output_tokens.shape[0], 1), tokenizer.bos_token_id, dtype=np.int32
            )
            input_tokens = np.concatenate([bos_tokens, output_tokens[:, :-1]], axis=-1)
            input_mask = np.concatenate(
                [prefix.attention_mask, inputs.attention_mask], axis=1
            )
            if FLAGS.loglikelihood_add_bos_token:
                bos_mask = np.ones_like(input_mask[:, :1])
            else:
                bos_mask = np.zeros_like(input_mask[:, :1])

            input_mask = np.concatenate([bos_mask, input_mask[:, :-1]], axis=1)
            output_mask = np.concatenate(
                [np.zeros_like(prefix.attention_mask), inputs.attention_mask], axis=1
            )
            batch = dict(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                input_mask=input_mask,
                output_mask=output_mask,
            )
            with mesh:
                loglikelihood, is_greedy, sharded_rng = forward_loglikelihood(
                    params, sharded_rng, batch
                )
                loglikelihood, is_greedy = jax.device_get((loglikelihood, is_greedy))
            return loglikelihood, is_greedy

        @staticmethod
        def loglikelihood_rolling(text):
            nonlocal sharded_rng
            inputs = tokenizer(
                text,
                padding="longest",
                truncation=False,
                max_length=np.iinfo(np.int32).max,
                return_tensors="np",
            )
            batch_size = inputs.input_ids.shape[0]
            output_tokens = inputs.input_ids
            attention_mask = inputs.attention_mask

            if output_tokens.shape[1] < FLAGS.seq_length:
                padding_length = FLAGS.seq_length - output_tokens.shape[1]
                pad_tokens = np.full(
                    (batch_size, padding_length), tokenizer.pad_token_id, dtype=np.int32
                )
                output_tokens = np.concatenate([output_tokens, pad_tokens], axis=-1)
                pad_mask = np.zeros(
                    (batch_size, padding_length), dtype=inputs.attention_mask.dtype
                )
                attention_mask = np.concatenate([attention_mask, pad_mask], axis=-1)

            bos_tokens = np.full(
                (batch_size, 1), tokenizer.bos_token_id, dtype=np.int32
            )
            input_tokens = np.concatenate([bos_tokens, output_tokens[:, :-1]], axis=-1)
            bos_mask = np.ones((batch_size, 1), dtype=inputs.attention_mask.dtype)
            total_seq_length = output_tokens.shape[1]

            total_loglikelihood = 0.0
            total_is_greedy = True
            # Sliding window
            for i in range(0, total_seq_length, FLAGS.seq_length):
                # Last window
                if i + FLAGS.seq_length > total_seq_length:
                    last_output_mask = np.copy(attention_mask[:, -FLAGS.seq_length :])
                    last_output_mask[:, : i - total_seq_length] = 0.0

                    batch = dict(
                        input_tokens=input_tokens[:, -FLAGS.seq_length :],
                        output_tokens=output_tokens[:, -FLAGS.seq_length :],
                        input_mask=attention_mask[:, -FLAGS.seq_length :],
                        output_mask=last_output_mask,
                    )

                # Normal window
                else:
                    batch = dict(
                        input_tokens=input_tokens[:, i : i + FLAGS.seq_length],
                        output_tokens=output_tokens[:, i : i + FLAGS.seq_length],
                        input_mask=attention_mask[:, i : i + FLAGS.seq_length],
                        output_mask=attention_mask[:, i : i + FLAGS.seq_length],
                    )

                with mesh:
                    loglikelihood, is_greedy, sharded_rng = forward_loglikelihood(
                        params, sharded_rng, batch
                    )
                    loglikelihood, is_greedy = jax.device_get(
                        (loglikelihood, is_greedy)
                    )

                total_loglikelihood += loglikelihood
                total_is_greedy = np.logical_and(is_greedy, total_is_greedy)

            return total_loglikelihood, total_is_greedy

        @staticmethod
        def generate(text):
            nonlocal sharded_rng
            inputs = prefix_tokenizer(
                text,
                padding="max_length",
                truncation=True,
                max_length=FLAGS.input_length,
                return_tensors="np",
            )
            batch = dict(
                input_tokens=inputs.input_ids,
                attention_mask=inputs.attention_mask,
            )
            with mesh:
                output, sharded_rng = forward_generate(params, sharded_rng, batch)
                output = jax.device_get(output)
            output_text = []
            for text in list(tokenizer.batch_decode(output)):
                if tokenizer.eos_token in text:
                    text = text.split(tokenizer.eos_token, maxsplit=1)[0]
                output_text.append(text)

            return output_text

        @staticmethod
        def greedy_until(prefix_text, until, max_length):
            nonlocal sharded_rng
            all_outputs = []
            for pf, ut in zip(prefix_text, until):
                if isinstance(ut, str):
                    ut = [ut]
                total_length = 0
                total_generated = ""

                while total_length < max_length:
                    pf_tokens = tokenizer(
                        pf,
                        padding=False,
                        truncation=False,
                        max_length=np.iinfo(np.int32).max,
                        return_tensors="np",
                    )
                    input_tokens = pf_tokens.input_ids
                    attention_mask = pf_tokens.attention_mask

                    if input_tokens.shape[1] < FLAGS.input_length:
                        extra = FLAGS.input_length - input_tokens.shape[1]
                        pad_tokens = np.full(
                            (1, extra), tokenizer.pad_token_id, dtype=np.int32
                        )
                        input_tokens = np.concatenate(
                            [pad_tokens, input_tokens], axis=1
                        )
                        pad_attention = np.zeros((1, extra), dtype=attention_mask.dtype)
                        attention_mask = np.concatenate(
                            [pad_attention, attention_mask], axis=1
                        )
                    elif input_tokens.shape[1] > FLAGS.input_length:
                        input_tokens = input_tokens[:, -FLAGS.input_length :]
                        attention_mask = attention_mask[:, -FLAGS.input_length :]

                    batch = dict(
                        input_tokens=input_tokens, attention_mask=attention_mask
                    )

                    with mesh:
                        output, sharded_rng = forward_greedy_generate(
                            params, sharded_rng, batch
                        )
                        output = jax.device_get(output)

                    total_length += output.shape[1]
                    output_text = tokenizer.batch_decode(output)[0]
                    total_generated = total_generated + output_text
                    pf = pf + output_text

                    done = False
                    for s in ut:
                        if s in total_generated:
                            total_generated = total_generated.split(s, maxsplit=1)[0]
                            done = True
                    if done:
                        break

                all_outputs.append(total_generated)

            return all_outputs

    server = OPTServer(FLAGS.lm_server)
    server.run()


if __name__ == "__main__":
    absl.app.run(main)
