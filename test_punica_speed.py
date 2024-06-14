import os
import math
import json
import torch
import matplotlib.pyplot as plt
import numpy as np
import vllm.lora.punica as punica  # type: ignore

# jee ops
# from jee_ops.sgmv_expand import sgmv_expand
# from jee_ops.sgmv_shrink import sgmv_shrink
from jee_ops.bgmv_expand import bgmv_expand
from jee_ops.bgmv_shrink import bgmv_shrink

# further ai ops
from furtherai_ops.sgmv_triton import sgmv_expand as f_sgmv_expand
from furtherai_ops.sgmv_triton import sgmv_shrink as f_sgmv_shrink

# The current punica kernel supports dimension and adds a dimension of 3424.
HIDDEN_SIZES = [
    128,
    256,
    512,
    1024,
    1152,
    1280,
    1536,
    2048,
    2304,
    2560,
    2752,
    3072,
    3424,
    3456,
    3584,
    4096,
    4608,
    5120,
    5504,
    5632,
    6144,
    6848,
    6912,
    7168,
    8192,
    9216,
    10240,
    11008,
    13824,
    14336,
    15360,
    22016,
    24576,
    27392,
    27648,
    32000,
    32256,
    32512,
    32768,
    33024,
    36864,
    43264,
    49152,
    64000,
    64256,
    102400,
    102656,
    128000,
    128256,
]
BATCHS = [i for i in range(0, 64, 8)]
NUM_LORA = [1, 4, 8, 16, 32, 64, 128, 256]
DTYPES = [torch.half]
MAX_RANKS = [1, 4, 8, 16, 32, 64, 128]
SCALES = [1.0]
OP_TYPES = ["shrink", "expand"]
SEED = [0]
CUDA_DEVICES = [f"cuda:{0}"]

rank = 16
seq_len = 1  #
num_loras = 8  # Arbitrary values for testing

num_call = 3


def assert_close(a, b):
    rtol, atol = {
        torch.float16: (1e-2, 1e-2),
        torch.bfloat16: (12e-2, 1e-2),
        torch.float32: (1e-2, 1e-2),
    }[a.dtype]
    torch.testing.assert_close(a, b, rtol=rtol, atol=atol)


@torch.inference_mode()
def _punica_bgmv(out_tensor, inputs, lora_weights, indices, scaling):
    layer_idx = 0
    punica.bgmv(out_tensor, inputs, lora_weights, indices, layer_idx, scaling)
    return


def _generate_data(
    batchs, hidden_size, lora_nums, max_rank, max_length, dtype, op_type, device
):
    # seq_len_tensor = torch.randint(1, max_length, (batchs,)).to(device)
    seq_len_tensor = torch.ones((batchs,), dtype=torch.int64).to(device) * max_length
    b_seq_start_loc = torch.cumsum(
        torch.tensor([0] + seq_len_tensor[:-1].tolist(), dtype=torch.long),
        dim=0,
    ).to(device)
    total_tokens = seq_len_tensor.sum()
    if op_type == "shrink":
        inputs_tensor = torch.rand((total_tokens, hidden_size), dtype=dtype).to(device)
        lora_weights = torch.rand(
            (lora_nums, max_rank, hidden_size),  # col-major
            dtype=dtype,
        ).to(device)
        # shrink op need atomic_add, so output is initinized by 0
        ref_out_tensor = torch.zeros(
            (total_tokens, max_rank), dtype=dtype, device=inputs_tensor.device
        )
        # NOTE  shrink kernel using torch.float32 as output type
        our_out_tensor = torch.zeros(
            (total_tokens, max_rank),
            dtype=torch.float32,
            device=inputs_tensor.device,
        )
    else:
        inputs_tensor = torch.rand(
            (total_tokens, max_rank),
            dtype=dtype,
        ).to(device)
        lora_weights = torch.rand(
            (lora_nums, hidden_size, max_rank),  # col-major
            dtype=dtype,
        ).to(device)
        # expand op needs to complete y+=a@lora_b, so output is
        # initinized randomly
        ref_out_tensor = torch.rand(
            (total_tokens, hidden_size),
            dtype=dtype,
            device=inputs_tensor.device,
        )
        # Ensure the same input.
        our_out_tensor = ref_out_tensor.clone()

    lora_indices_tensor = torch.randint(
        0, lora_nums - 1 if lora_nums > 1 else 1, (batchs,)
    ).to(device)
    # lora_indices_tensor = torch.randint(
    #     0, 1, (batchs,)
    # ).to(device)

    indices = torch.zeros((total_tokens), dtype=torch.long).to(device)
    current_offset = 0
    for b_id in range(batchs):
        lora_index = lora_indices_tensor[b_id]
        indices[current_offset : current_offset + seq_len_tensor[b_id]] = (
            lora_index.item()
        )
        current_offset += seq_len_tensor[b_id].item()
    return (
        inputs_tensor,
        lora_weights,
        our_out_tensor,
        ref_out_tensor,
        b_seq_start_loc,
        lora_indices_tensor,
        seq_len_tensor,
        indices,
    )


def test_triton_bgmv_punica_bgmv(
    hidden_size: int,
    batchs: int,
    scaling: float,
    dtype: torch.dtype,
    op_type: str,
    seed: int,
    device: str,
):
    # avoid `No suitable kernel. h_in=xx h_out=xxxx ` error
    if dtype == torch.float32 or hidden_size == 3424:
        return
    torch.manual_seed(seed)
    if batchs == 0:
        batchs += 1

    # data for jee ops and punica ops
    (
        inputs_tensor,
        lora_weights,
        our_out_tensor,
        ref_out_tensor,
        b_seq_start_loc,
        lora_indices_tensor,
        seq_len_tensor,
        indices,
    ) = _generate_data(
        batchs, hidden_size, num_loras, rank, seq_len, dtype, op_type, device
    )
    lora_weights_4d = lora_weights.unsqueeze(dim=1)

    # data for further ai ops
    further_out_tensor = our_out_tensor.clone()
    out_col_offset = 0
    repeats_per_lora = seq_len
    S = batchs * seq_len
    S = math.ceil(S / repeats_per_lora) * repeats_per_lora
    R = rank
    H = hidden_size
    num_unique = S // repeats_per_lora
    ranks = torch.full((S,), R, device="cuda", dtype=torch.int32)
    f_indices = torch.arange(num_unique, device="cuda")
    repeats = torch.full(
        (num_unique,), repeats_per_lora, device="cuda", dtype=torch.int32
    )
    repeats = torch.cat(
        [torch.zeros((1,), device="cuda", dtype=torch.int32), repeats.cumsum(dim=-1)]
    )
    # NOTE The ops needs to adjust the order of weights before computation,
    # otherwise, it cannot obtain the correct results aligned with punica
    f_lora_weights_4d = torch.cat(
        [lora_weights_4d[idx : idx + 1, ...] for idx in lora_indices_tensor], dim=0
    )

    #########################################################
    #                        shrink                         #
    #########################################################
    if op_type == "shrink":
        ################# jee bgmv_shrink bigen #############
        # warmup
        bgmv_shrink(
            inputs_tensor,
            lora_weights,
            our_out_tensor,
            lora_indices_tensor,
            scaling,
        )

        # cuda graph
        torch.cuda.synchronize()
        bgmv_shrink_graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(bgmv_shrink_graph):
            for _ in range(100):
                bgmv_shrink(
                    inputs_tensor,
                    lora_weights,
                    our_out_tensor,
                    lora_indices_tensor,
                    scaling,
                )
        torch.cuda.synchronize()

        # replay
        start_event.record()
        for _ in range(num_call):
            bgmv_shrink_graph.replay()
        end_event.record()
        end_event.synchronize()
        dur_ms = start_event.elapsed_time(end_event) / 100 / num_call
        bgmv_shrink_graph.reset()
        record["jee_bgmv_shrink"].append(
            dict(
                dur_ms=dur_ms,
                batchs=batchs,
                hidden_size=hidden_size,
                dtype=str(dtype),
                seed=seed,
            )
        )
        ################# jee bgmv_shrink end ###############

        ############ further ai sgmv_shrink bigen ###########
        # warmup
        f_sgmv_shrink(
            inputs_tensor,
            f_lora_weights_4d,
            torch.rand_like(further_out_tensor),
            ranks,
            f_indices,
            repeats,
            repeats_per_lora,
        )

        # cuda graph
        torch.cuda.synchronize()
        f_sgmv_shrink_graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(f_sgmv_shrink_graph):
            for _ in range(100):
                f_sgmv_shrink(
                    inputs_tensor,
                    f_lora_weights_4d,
                    further_out_tensor,
                    ranks,
                    f_indices,
                    repeats,
                    repeats_per_lora,
                )
        torch.cuda.synchronize()

        # replay
        start_event.record()
        for _ in range(num_call):
            f_sgmv_shrink_graph.replay()
        end_event.record()
        end_event.synchronize()
        dur_ms = start_event.elapsed_time(end_event) / 100 / num_call
        f_sgmv_shrink_graph.reset()
        record["further_bgmv_shrink"].append(
            dict(
                dur_ms=dur_ms,
                batchs=batchs,
                hidden_size=hidden_size,
                dtype=str(dtype),
                seed=seed,
            )
        )
        ############ further ai sgmv_shrink end #############

        ############# punica bgmv_shrink begin ##############
        # warmup
        _punica_bgmv(
            ref_out_tensor,
            inputs_tensor,
            lora_weights_4d,
            indices,
            scaling if op_type == "shrink" else 1.0,
        )

        # cuda graph
        torch.cuda.synchronize()
        punica_bgmv_graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(punica_bgmv_graph):
            for _ in range(100):
                _punica_bgmv(
                    ref_out_tensor,
                    inputs_tensor,
                    lora_weights_4d,
                    indices,
                    scaling if op_type == "shrink" else 1.0,
                )
        torch.cuda.synchronize()

        # replay
        start_event.record()
        for _ in range(num_call):
            punica_bgmv_graph.replay()
        end_event.record()
        end_event.synchronize()
        dur_ms = start_event.elapsed_time(end_event) / 100 / num_call
        punica_bgmv_graph.reset()
        record["punica_bgmv_shrink"].append(
            dict(
                dur_ms=dur_ms,
                batchs=batchs,
                hidden_size=hidden_size,
                dtype=str(dtype),
                seed=seed,
            )
        )
        ############## punica bgmv_shrink end ###############

        ################# assert_close start ################
        our_out_tensor = torch.zeros_like(our_out_tensor)
        bgmv_shrink(
            inputs_tensor,
            lora_weights,
            our_out_tensor,
            lora_indices_tensor,
            scaling,
        )

        further_out_tensor = torch.zeros_like(further_out_tensor)
        f_sgmv_shrink(
            inputs_tensor,
            f_lora_weights_4d,
            further_out_tensor,
            ranks,
            f_indices,
            repeats,
            repeats_per_lora,
        )

        ref_out_tensor = torch.zeros_like(ref_out_tensor)
        _punica_bgmv(
            ref_out_tensor,
            inputs_tensor,
            lora_weights_4d,
            indices,
            scaling if op_type == "shrink" else 1.0,
        )

        assert_close(our_out_tensor, ref_out_tensor.float())
        assert_close(our_out_tensor, further_out_tensor)
        ################# assert_close end ##################

    #########################################################
    #                        expand                         #
    #########################################################
    else:
        ################# jee bgmv_expand bigen #############
        # warmup
        bgmv_expand(
            inputs_tensor,
            lora_weights,
            our_out_tensor,
            lora_indices_tensor,
            add_inputs=True,
        )

        # cuda graph
        torch.cuda.synchronize()
        bgmv_expand_graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(bgmv_expand_graph):
            for _ in range(100):
                bgmv_expand(
                    inputs_tensor,
                    lora_weights,
                    our_out_tensor,
                    lora_indices_tensor,
                    add_inputs=True,
                )
        torch.cuda.synchronize()

        # replay
        start_event.record()
        for _ in range(num_call):
            bgmv_expand_graph.replay()
        end_event.record()
        end_event.synchronize()
        dur_ms = start_event.elapsed_time(end_event) / 100 / num_call
        bgmv_expand_graph.reset()
        record["jee_bgmv_expand"].append(
            dict(
                dur_ms=dur_ms,
                batchs=batchs,
                hidden_size=hidden_size,
                dtype=str(dtype),
                seed=seed,
            )
        )
        ################# jee bgmv_expand end ###############

        ############ further ai sgmv_expand bigen ###########
        # warmup
        f_sgmv_expand(
            inputs_tensor,
            f_lora_weights_4d,
            torch.rand((S, H + out_col_offset), device="cuda", dtype=dtype),
            ranks,
            f_indices,
            repeats,
            repeats_per_lora,
            out_col_offset=out_col_offset,
        )

        # cuda graph
        torch.cuda.synchronize()
        f_sgmv_expand_graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(f_sgmv_expand_graph):
            for _ in range(100):
                f_sgmv_expand(
                    inputs_tensor,
                    f_lora_weights_4d,
                    further_out_tensor,
                    ranks,
                    f_indices,
                    repeats,
                    repeats_per_lora,
                    out_col_offset=out_col_offset,
                )
        torch.cuda.synchronize()

        # replay
        start_event.record()
        for _ in range(num_call):
            f_sgmv_expand_graph.replay()
        end_event.record()
        end_event.synchronize()
        dur_ms = start_event.elapsed_time(end_event) / 100 / num_call
        f_sgmv_expand_graph.reset()
        record["further_bgmv_expand"].append(
            dict(
                dur_ms=dur_ms,
                batchs=batchs,
                hidden_size=hidden_size,
                dtype=str(dtype),
                seed=seed,
            )
        )
        ############ further ai sgmv_expand end #############

        ############# punica bgmv_expand begin ##############
        # warmup
        _punica_bgmv(
            ref_out_tensor,
            inputs_tensor,
            lora_weights_4d,
            indices,
            scaling if op_type == "shrink" else 1.0,
        )

        # cuda graph
        torch.cuda.synchronize()
        _punica_bgmv_graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(_punica_bgmv_graph):
            for _ in range(100):
                _punica_bgmv(
                    ref_out_tensor,
                    inputs_tensor,
                    lora_weights_4d,
                    indices,
                    scaling if op_type == "shrink" else 1.0,
                )
        torch.cuda.synchronize()

        # replay
        start_event.record()
        for _ in range(num_call):
            _punica_bgmv_graph.replay()
        end_event.record()
        end_event.synchronize()
        dur_ms = start_event.elapsed_time(end_event) / 100 / num_call
        _punica_bgmv_graph.reset()
        record["punica_bgmv_expand"].append(
            dict(
                dur_ms=dur_ms,
                batchs=batchs,
                hidden_size=hidden_size,
                dtype=str(dtype),
                seed=seed,
            )
        )
        ############# punica bgmv_expand end ################

        ################# assert_close start ################
        our_out_tensor = torch.zeros_like(our_out_tensor)
        bgmv_expand(
            inputs_tensor,
            lora_weights,
            our_out_tensor,
            lora_indices_tensor,
            add_inputs=True,
        )

        further_out_tensor = torch.zeros_like(further_out_tensor)
        f_sgmv_expand(
            inputs_tensor,
            f_lora_weights_4d,
            further_out_tensor,
            ranks,
            f_indices,
            repeats,
            repeats_per_lora,
            out_col_offset=out_col_offset,
        )

        ref_out_tensor = torch.zeros_like(ref_out_tensor)
        _punica_bgmv(
            ref_out_tensor,
            inputs_tensor,
            lora_weights_4d,
            indices,
            scaling if op_type == "shrink" else 1.0,
        )

        assert_close(our_out_tensor, ref_out_tensor)
        assert_close(our_out_tensor, further_out_tensor)
        ################# assert_close end ##################

    # save
    with open(
        os.path.join(os.path.dirname(__file__), "record_bgmv_lee_cudagraph.json"),
        "w",
        encoding="utf-8",
    ) as s:
        json.dump(record, s, indent=4, ensure_ascii=False)


def plot_results(bs=1, op_types="shrink"):
    results = json.load(
        open(
            os.path.join(os.path.dirname(__file__), "record_bgmv_lee_cudagraph.json"),
            "r",
        )
    )

    jee_bgmv = results[f"jee_bgmv_{op_types}"]
    punica_bgmv = results[f"punica_bgmv_{op_types}"]
    further_bgmv = results[f"further_bgmv_{op_types}"]

    hidden_size = np.array([d["hidden_size"] for d in jee_bgmv if d["batchs"] == bs])[
        ::2
    ]
    jee_bgmv = np.array([d["dur_ms"] for d in jee_bgmv if d["batchs"] == bs])[::2]
    punica_bgmv = np.array([d["dur_ms"] for d in punica_bgmv if d["batchs"] == bs])[::2]
    further_bgmv = np.array([d["dur_ms"] for d in further_bgmv if d["batchs"] == bs])[
        ::2
    ]

    plt.figure(figsize=(10, 6))
    plt.plot(
        hidden_size,
        jee_bgmv,
        "o-",
        label=f"jee_bgmv_{op_types} bachsize={bs}",
        color="red",
    )
    plt.plot(
        hidden_size,
        punica_bgmv,
        "o-",
        label=f"punica_bgmv_{op_types} bachsize={bs}",
        color="orange",
    )
    plt.plot(
        hidden_size,
        further_bgmv,
        "o-",
        label=f"further_bgmv_{op_types} bachsize={bs}",
        color="blue",
    )
    # NOTE change GPU name
    plt.title(
        f"bgmv(ours) vs. bgmv(further ai) with rank={rank} seq_len={seq_len} on NVIDIA RTX3090"
    )
    plt.xlabel("Hidden size")
    plt.ylabel("Duration (ms)")

    plt.legend()

    plt.grid(True)
    plt.savefig(
        os.path.join(os.path.dirname(__file__), f"bgmv_{op_types}_bachsize{bs}.png")
    )


if __name__ == "__main__":
    from itertools import product

    start_event = torch.cuda.Event(enable_timing=True)
    end_event = torch.cuda.Event(enable_timing=True)
    record = {
        "jee_bgmv_shrink": [],
        "punica_bgmv_shrink": [],
        "further_bgmv_shrink": [],
        "jee_bgmv_expand": [],
        "punica_bgmv_expand": [],
        "further_bgmv_expand": [],
    }

    lst = list(
        product(
            HIDDEN_SIZES,
            BATCHS,
            SCALES,
            DTYPES,
            OP_TYPES,
            SEED,
            CUDA_DEVICES,
        )
    )

    for hidden_size, bs, scaling, dtype, op_type, seed, device in lst:
        test_triton_bgmv_punica_bgmv(
            hidden_size, bs, scaling, dtype, op_type, seed, device
        )

    for bs in BATCHS:
        for op_types in OP_TYPES:
            plot_results(bs=bs if bs != 0 else 1, op_types=op_types)
