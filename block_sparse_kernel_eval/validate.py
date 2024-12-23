import os
import math
import torch
from torch.nn import functional as F
from torch.utils.cpp_extension import load
from block_sparse_kernel_eval.kernels import get_file_with_settings

os.environ["TORCH_CUDA_ARCH_LIST"] = "8.0"
torch.manual_seed(42)

implementations = [get_file_with_settings("bsa_shared_memory_coalescing", "extension/bsa_shared_memory_coalescing.cu")]

T = 1024
D = 64
B = 8
block_size = 32

# NOTE: need to make sure that configuration settings line up

num_query_blocks = (T + block_size - 1) // block_size
num_blocks_selected = 2  # Number of blocks selected per query block

q = torch.randn(B, T, D).cuda()
k = torch.randn(B, T, D).cuda()
v = torch.randn(B, T, D).cuda()
block_indices = torch.randint(0, num_query_blocks, (B, num_query_blocks, num_blocks_selected)).cuda().int()

print("Block indices shape: ", block_indices.shape)

print('=== profiling manual attention ===')

def baseline_block_sparse_attention(q, k, v, block_indices, block_size):
    B, T, D = q.shape
    O = torch.zeros_like(v)
    num_query_blocks = (T + block_size - 1) // block_size

    for b in range(B):
        for query_block_index in range(num_query_blocks):
            query_block = q[b, query_block_index * block_size : (query_block_index + 1) * block_size, :]
            key_blocks = []
            value_blocks = []
            current_block_indices = block_indices[b, query_block_index, :].tolist()

            for block_index in current_block_indices:
                key_block = k[b, block_index * block_size : (block_index + 1) * block_size, :]
                key_blocks.append(key_block)
                value_block = v[b, block_index * block_size : (block_index + 1) * block_size, :]
                value_blocks.append(value_block)

            key_block = torch.cat(key_blocks, dim=0)
            value_block = torch.cat(value_blocks, dim=0)

            attention = torch.matmul(query_block, key_block.transpose(-2, -1))
            attention = torch.nn.functional.softmax(attention, dim=-1)
            output = torch.matmul(attention, value_block)

            O[b, query_block_index * block_size : (query_block_index + 1) * block_size, :] = output
    return O

with torch.autograd.profiler.profile(use_cuda=True, record_shapes=False, with_stack=False) as prof:
    manual_result = baseline_block_sparse_attention(q, k, v, block_indices, block_size)
    print(manual_result.flatten()[:10])
# print(prof.key_averages().table(sort_by="cuda_time_total", row_limit=10))

print('=== profiling custom cuda block flash attention ===')

for implementation_idx, implementation in enumerate(implementations):
  print(f"Profiling implementation {implementation_idx}")
  with torch.autograd.profiler.profile(use_cuda=True, record_shapes=False, with_stack=False) as prof:
      minimal_result = implementation.forward(q, k, v, block_indices)
      print(minimal_result.flatten()[:10])
  print(prof.key_averages().table(sort_by='cuda_time_total', row_limit=10))
  print('attention values sanity check:', torch.allclose(minimal_result, manual_result, rtol=0, atol=1e-01)) # need more tolerance for mixed precision