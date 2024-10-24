from typing import Union, Tuple, List, Dict

import math
import torch
import torch.nn as nn
from torch import Tensor


def segment_sum(data, segment_ids, num_segments):
    """
    Args:
        data (Tensor): A tensor, typically two-dimensional.
        segment_ids (Tensor): A one-dimensional tensor that indicates the 
            segmentation in data.
        num_segments (int): Total segments.

    Returns:
        output: sum calculated by segment_ids, which has the same shape as data.
    """
    output = torch.zeros((num_segments, data.size(1)), device=data.device, dtype=data.dtype)
    output.scatter_add_(0, segment_ids.unsqueeze(1).expand(-1, data.size(1)), data)

    return output


def segment_softmax(
    data: Tensor,
    segment_ids: Tensor,
    num_segments: int
):
    """
    Args:
        data (Tensor): A tensor, typically two-dimensional.
        segment_ids (Tensor): A one-dimensional tensor that indicates the 
            segmentation in data.
        num_segments (int): Total segments.

    Returns:
        score: softmax score, which has the same shape as data.
    """
    max_values = torch.zeros(num_segments, data.size(1), device=data.device, dtype=data.dtype)
    for i in range(num_segments):
        segment_data = data[segment_ids == i]
        if segment_data.size(0) > 0:
            max_values[i] = segment_data.max(dim=0)[0]
    
    gathered_max_values = max_values[segment_ids]
    # print(gathered_max_values)
    exp = torch.exp(data - gathered_max_values)
    # print(gathered_max_values)
    # print(data - gathered_max_values)
    # print(exp)

    denominator = torch.zeros(num_segments, data.size(1), device=data.device)
    for i in range(num_segments):
        # print(exp[segment_ids == i])
        # print(exp[segment_ids == i].sum(dim=0))
        segment_exp = exp[segment_ids == i]
        if segment_exp.size(0) > 0:
            denominator[i] = segment_exp.sum(dim=0)

    gathered_denominator = denominator[segment_ids]
    # print(gathered_denominator)
    score = exp / (gathered_denominator + 1e-16)

    return score


class HGTConv(torch.nn.Module):
    def __init__(
        self,
        in_channels: Union[int, Dict[str, int]],
        out_channels: int,
        metadata: Tuple[List[str], List[Tuple[str, str]]],
        heads: int = 1,
        group: str = "sum",
        dropout_rate: float = 0.,
    ):
        super().__init__()

        if not isinstance(in_channels, dict):
            in_channels = {node_type: in_channels for node_type in metadata[0]}
        
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.heads = heads
        self.group = group

        self.q_lin = nn.ModuleDict()
        self.k_lin = nn.ModuleDict()
        self.v_lin = nn.ModuleDict()
        self.a_lin = nn.ModuleDict()
        self.skip = nn.ParameterDict()
        self.dropout_rate = dropout_rate
        self.dropout = nn.Dropout(self.dropout_rate)
    
        # Initialize parameters for each node type
        for node_type, in_channels in in_channels.items():
            self.q_lin[node_type] = nn.Linear(in_features=in_channels, out_features=out_channels)
            self.k_lin[node_type] = nn.Linear(in_features=in_channels, out_features=out_channels)
            self.v_lin[node_type] = nn.Linear(in_features=in_channels, out_features=out_channels)
            self.a_lin[node_type] = nn.Linear(in_features=out_channels, out_features=out_channels)
            self.skip[node_type] = nn.Parameter(torch.tensor(1.0))
        
        self.a_rel = nn.ParameterDict()
        self.m_rel = nn.ParameterDict()
        self.p_rel = nn.ParameterDict()
        dim = out_channels // heads

        # Initialize parameters for each edge type
        for edge_type in metadata[1]:
            edge_type = '__'.join(edge_type)

            # Initialize a_rel weights with truncated normal
            a_weight = nn.Parameter(torch.empty((heads, dim, dim), requires_grad=True))
            nn.init.trunc_normal_(a_weight)
            self.a_rel[edge_type + 'a'] = a_weight

            # Initialize m_rel weights with truncated normal
            m_weight = nn.Parameter(torch.empty((heads, dim, dim), requires_grad=True))
            nn.init.trunc_normal_(m_weight)
            self.m_rel[edge_type + 'm'] = m_weight

            # Initialize p_rel weights with ones
            self.p_rel[edge_type] = nn.Parameter(torch.ones(heads))
    
    def forward(
        self,
        x_dict: Dict[str, Tensor],
        edge_index_dict: Dict[Tuple[str, str], Tensor], # sparse_coo here!
    ):
        H, D = self.heads, self.out_channels // self.heads
        k_dict, q_dict, v_dict, out_dict = {}, {}, {}, {}

        # Prepare q, k, v by node types
        for node_type, x in x_dict.items():
            k_dict[node_type] = self.k_lin[node_type](x).view(-1, H, D)
            q_dict[node_type] = self.q_lin[node_type](x).view(-1, H, D)
            v_dict[node_type] = self.v_lin[node_type](x).view(-1, H, D)
            out_dict[node_type] = []

        # Iterate over edge-types:
        for edge_type, edge_index in edge_index_dict.items():
            src_type, dst_type = edge_type[0], edge_type[1]
            edge_type = '__'.join(edge_type)

            # a_rel: (H, D, D), k: (B, H, D)
            a_rel = self.a_rel[edge_type + 'a']
            k = (k_dict[src_type].transpose(1, 0) @ a_rel).transpose(1, 0)
            
            # m_rel: (H, D, D), v: (B, H, D)
            m_rel = self.m_rel[edge_type + 'm']
            v = (v_dict[src_type].transpose(1, 0) @ m_rel).transpose(1, 0)

            edge_index = edge_index.coalesce()
            src_index = edge_index.indices()[0]
            tgt_index = edge_index.indices()[1]
            # q_i/v_j/k_j: (N, H, D) N is edge_index's shape[1]. rel: (H,)
            q_i = torch.index_select(q_dict[dst_type], dim=0, index=tgt_index)
            k_j = torch.index_select(k, dim=0, index=src_index)
            v_j = torch.index_select(v, dim=0, index=src_index)
            rel = self.p_rel[edge_type]
            # out: (N'[N after deduplication], out_channels)
            out = self.propagate(
                edge_index=edge_index,
                aggr='sum',
                q_i=q_i,
                k_j=k_j,
                v_j=v_j,
                rel=rel,
                num_nodes=x_dict[dst_type].shape[0]
            )
            out_dict[dst_type].append(out)

        for node_type, outs in out_dict.items():
            outs = torch.stack(outs)
            # out: (N', out_channels)
            out = torch.sum(outs, dim=0, keepdim=False)
            # out: (N', out_channels)
            out = self.a_lin[node_type](out)
            alpha = torch.sigmoid(self.skip[node_type])
            # print(self.skip[node_type])
            # print(alpha)
            # print(out.shape)
            # print(x_dict[node_type].shape)
            # out: (N', out_channels)
            out = alpha * out + (1 - alpha) * x_dict[node_type]
            out_dict[node_type] = out
        # out_dict: (Num_node_type, N', out_channels)
        return out_dict

        
    def propagate(
        self,
        edge_index: Tensor,
        aggr: str,
        q_i: Tensor,
        k_j: Tensor,
        v_j: Tensor,
        rel: Tensor,
        num_nodes: int,
    ):
        msg = self.message(q_i, k_j, v_j, rel, edge_index.indices()[1], num_nodes)
        # x: (N'[N after deduplication], out_channels)
        # print(msg)
        # print(edge_index.indices()[1])
        x = self.aggregate(msg, edge_index.indices()[1], num_nodes, aggr)
        return x

    def message(self, q_i, k_j, v_j, rel, tgt_index, num_nodes):
        # alpha: (N, H)
        alpha = (k_j * q_i).sum(dim=-1) * rel
        alpha = alpha / math.sqrt(q_i.size(-1))
        alpha = self.dropout(segment_softmax(alpha, tgt_index, num_nodes))
        # out: (N, H, D)
        out = v_j * alpha.unsqueeze(-1)
        return out.view(-1, self.out_channels) # (N, out_channels[H*D])
    
    def aggregate(self, msg, tgt_index, num_nodes, aggr):
        if aggr == 'sum':
            return segment_sum(msg, tgt_index, num_nodes)