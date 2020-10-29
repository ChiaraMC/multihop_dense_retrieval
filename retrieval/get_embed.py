import collections
import logging
import json
import os
import random
from tqdm import tqdm
import numpy as np
import torch
from copy import deepcopy

from torch.utils.data import DataLoader
from data.encode_datasets import EmDataset, em_collate
from models.retriever import CtxEncoder, RobertaCtxEncoder
from transformers import AutoConfig, AutoTokenizer
from utils.utils import move_to_cuda, load_saved
from config import encode_args


def main():
    args = encode_args()
    if args.fp16:
        import apex
        apex.amp.register_half_function(torch, 'einsum')


    if args.local_rank == -1 or args.no_cuda:
        device = torch.device(
            "cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        torch.distributed.init_process_group(backend='nccl')

    if not args.predict_file:
        raise ValueError(
            "If `do_predict` is True, then `predict_file` must be specified.")

    bert_config = AutoConfig.from_pretrained(args.model_name)

    if "roberta" in args.model_name:
        model = RobertaCtxEncoder(bert_config, args)
    else:
        model = CtxEncoder(bert_config, args)
    
    tokenizer = AutoTokenizer.from_pretrained(args.model_name)

    eval_dataset = EmDataset(
        tokenizer, args.predict_file, args.max_q_len, args.max_c_len, args.is_query_embed)        
    eval_dataloader = DataLoader(
        eval_dataset, batch_size=args.predict_batch_size, collate_fn=em_collate, pin_memory=True, num_workers=args.num_workers)

    assert args.init_checkpoint != ""
    model = load_saved(model, args.init_checkpoint, exact=False)
    model.to(device)

    if args.fp16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError(
                "Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model = amp.initialize(model, opt_level=args.fp16_opt_level)

    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                                                          output_device=args.local_rank)
    elif n_gpu > 1:
        model = torch.nn.DataParallel(model)

    embeds = predict(model, eval_dataloader)
    print(embeds.size())
    np.save(args.embed_save_path, embeds.cpu().numpy())

def predict(model, eval_dataloader):
    if type(model) == list:
        model = [m.eval() for m in model]
    else:
        model.eval()

    embed_array = []
    for batch in tqdm(eval_dataloader):
        batch_to_feed = move_to_cuda(batch)
        with torch.no_grad():
            results = model(batch_to_feed)
            embed = results['embed'].cpu()
            embed_array.append(embed)

    ## linear combination tuning on dev data
    embed_array = torch.cat(embed_array)

    model.train()
    return embed_array


if __name__ == "__main__":
    main()
