import argparse
import os
import re
import time
import json
import torch
from transformers import AutoConfig, AutoTokenizer
from intel_extension_for_transformers.transformers import (
    AutoModelForCausalLM,
    AutoModel,
)
from intel_extension_for_transformers.transformers import (
    BitsAndBytesConfig,
    RtnConfig,
    AwqConfig,
    TeqConfig,
    GPTQConfig,
    AutoRoundConfig,
)

parser = argparse.ArgumentParser()
parser.add_argument("--model", default=None)
parser.add_argument(
    "--dataset", nargs="?", default="NeelNanda/pile-10k", const="NeelNanda/pile-10k"
)
parser.add_argument("--device", default="cpu")
parser.add_argument(
    "--max_new_tokens", default=32, type=int, help="output max new tokens"
)
parser.add_argument("--output_dir", nargs="?", default="./saved_results")
# ============Benchmark configs==============
parser.add_argument("--benchmark", action="store_true")
parser.add_argument("--iters", default=100, type=int, help="num iter")
parser.add_argument("--num_warmup", default=10, type=int, help="num warmup")
# ============Accuracy configs==============
parser.add_argument("--accuracy", action="store_true")
parser.add_argument("--batch_size", default=56, type=int, help="batch size num.")
parser.add_argument(
    "--tasks",
    default="lambada_openai",
    type=str,
    help="tasks list for accuracy validation",
)
# ============WeightOnlyQuant configs===============
parser.add_argument("--woq", action="store_true")
parser.add_argument(
    "--woq_algo",
    default="Rtn",
    choices=["Rtn", "Awq", "Teq", "GPTQ", "AutoRound"],
    help="Weight-only algorithm.",
)
parser.add_argument(
    "--bits",
    type=int,
    default=8,
    choices=[4, 8],
)
parser.add_argument(
    "--weight_dtype",
    type=str,
    default="int8",
    choices=[
        "int8",
        "int4_clip",
        "int4_fullrange",
        "fp4_e2m1_bnb",
        "fp4_e2m1",
        "nf4",
        "fp8_e5m2",
        "fp8_e4m3",
    ],
)
parser.add_argument(
    "--scale_dtype",
    type=str,
    default="fp32",
    choices=["fp32", "fp8"],
)
parser.add_argument(
    "--compute_dtype",
    type=str,
    default="fp32",
    choices=["fp32", "bf16", "int8"],
)
parser.add_argument("--group_size", type=int, default=128)
parser.add_argument("--scheme", default=None)
parser.add_argument(
    "--layer_wise",
    action="store_true",
    help="Use layer wise to do quantization",
)
parser.add_argument("--woq_loading", action="store_true")
# ============GPTQ configs==============
parser.add_argument(
    "--desc_act",
    action="store_true",
    help="Whether to apply the activation order GPTQ heuristic.",
)
parser.add_argument(
    "--damp_percent",
    type=float,
    default=0.01,
    help="Percent of the average Hessian diagonal to use for dampening.",
)
parser.add_argument(
    "--blocksize",
    type=int,
    default=128,
    help="Block size. sub weight matrix size to run GPTQ.",
)
parser.add_argument(
    "--nsamples", type=int, default=512, help="Number of calibration data samples."
)
parser.add_argument(
    "--max_input_length",
    type=int,
    default=2048,
    help="Calibration dataset sequence max length, this should align with your model config",
)
parser.add_argument(
    "--static_groups",
    action="store_true",
    help="Use determined group to do quantization",
)
# ============AUTOROUND configs==============
parser.add_argument(
    "--calib_len",
    type=int,
    default=2048,
    help="Calibration dataset sequence max length, this should align with your model config",
)
parser.add_argument(
    "--calib_iters",
    type=int,
    default=200,
    help="Calibration inference iterations",
)
parser.add_argument(
    "--lr",
    type=float,
    default=None,
    help="learning rate, if None, it will be set to 1.0/iters automatically",
)
parser.add_argument(
    "--minmax_lr",
    type=float,
    default=None,
    help="minmax learning rate, if None,it will beset to be the same with lr",
)
parser.add_argument(
    "--use_quant_input",
    action="store_true",
    help="whether to use the output of quantized block to tune the next block",
)

# ============BitsAndBytes configs==============
parser.add_argument("--bitsandbytes", action="store_true")
# ============AutoModel parameters==============
parser.add_argument("--load_in_4bit", action="store_true")
parser.add_argument("--load_in_8bit", action="store_true")
parser.add_argument("--_commit_hash", default=None, type=str)
parser.add_argument("--trust_remote_code", action="store_true")
parser.add_argument("--use_neural_speed", action="store_true")
# =======================================
args = parser.parse_args()
args.scheme = "asym" if args.scheme is None and args.woq_algo == "AutoRound" else "sym" 

config = AutoConfig.from_pretrained(
    args.model,
    torchscript=(
        True
        if args.woq_algo in ["Awq", "Teq"]
        else False
    ),  # torchscript will force `return_dict=False` to avoid jit errors
    use_cache=True,  # to use kv cache.
    trust_remote_code=args.trust_remote_code,
    _commit_hash=args._commit_hash,
)

# chatglm
if config.model_type == "chatglm":
    AutoModelForCausalLM = AutoModel

# tokenizer
if hasattr(config, "auto_map") and "chatglm2" in config.auto_map["AutoConfig"]:
    tokenizer = AutoTokenizer.from_pretrained(
        "THUDM/chatglm2-6b", trust_remote_code=True
    )
else:
    tokenizer = AutoTokenizer.from_pretrained(
        args.model, trust_remote_code=args.trust_remote_code
    )

# Generation
if args.use_neural_speed:
    generate_kwargs = dict(do_sample=False, temperature=0.9, num_beams=1)
else:
    generate_kwargs = dict(do_sample=False, temperature=0.9, num_beams=4)

# woq/bitsandbytes config setting
quantization_config = None
if args.woq:
    if args.woq_algo == "Rtn":
        quantization_config = RtnConfig(
            tokenizer=tokenizer,
            bits=args.bits,
            sym=True if args.scheme == "sym" else False,
            group_size=args.group_size,
            compute_dtype=args.compute_dtype,
            scale_dtype=args.scale_dtype,
            weight_dtype=args.weight_dtype,
            layer_wise=args.layer_wise,
        )
    elif args.woq_algo == "Awq":
        quantization_config = AwqConfig(
            tokenizer=tokenizer,
            dataset=args.dataset,
            bits=args.bits,
            zero_point=False if args.scheme == "sym" else True,
            group_size=args.group_size,
            max_input_length=args.max_input_length,
            compute_dtype=args.compute_dtype,
            scale_dtype=args.scale_dtype,
            weight_dtype=args.weight_dtype,
            calib_iters=args.calib_iters,
        )
    elif args.woq_algo == "Teq":
        quantization_config = TeqConfig(
            tokenizer=tokenizer,
            dataset=args.dataset,
            bits=args.bits,
            sym=True if args.scheme == "sym" else False,
            group_size=args.group_size,
            max_input_length=args.max_input_length,
            compute_dtype=args.compute_dtype,
            scale_dtype=args.scale_dtype,
            weight_dtype=args.weight_dtype,
            calib_iters=args.calib_iters,
        )
    elif args.woq_algo == "GPTQ":
        quantization_config = GPTQConfig(
            tokenizer=tokenizer,
            dataset=args.dataset,
            bits=args.bits,
            desc_act=args.desc_act,
            damp_percent=args.damp_percent,
            sym=True if args.scheme == "sym" else False,
            blocksize=args.blocksize,
            nsamples=args.nsamples,
            static_groups=args.static_groups,
            group_size=args.group_size,
            max_input_length=args.max_input_length,
            compute_dtype=args.compute_dtype,
            scale_dtype=args.scale_dtype,
            weight_dtype=args.weight_dtype,
            calib_iters=args.calib_iters,
            layer_wise=args.layer_wise,
        )
    elif args.woq_algo == "AutoRound":
        quantization_config = AutoRoundConfig(
            tokenizer=tokenizer,
            dataset=args.dataset,
            bits=args.bits,
            sym=True if args.scheme == "sym" else False,
            nsamples=args.nsamples,
            group_size=args.group_size,
            compute_dtype=args.compute_dtype,
            scale_dtype=args.scale_dtype,
            weight_dtype=args.weight_dtype,
            calib_iters=args.calib_iters,
            calib_len=args.calib_len,
            lr=args.lr,
            minmax_lr=args.minmax_lr,
            use_quant_input=args.use_quant_input,
        )
    else:
        assert False, "Please set the correct '--woq_algo'"
# bitsandbytes
elif args.bitsandbytes:
    quantization_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
    )
else:
    print("The quantization_config is None.")

# get optimized model
if quantization_config is not None:
    user_model = AutoModelForCausalLM.from_pretrained(
        args.model,
        quantization_config=quantization_config,
        trust_remote_code=args.trust_remote_code,
        _commit_hash=args._commit_hash,
        use_neural_speed=args.use_neural_speed,
    )
elif args.load_in_4bit or args.load_in_8bit:
    # CPU device usage is provided by intel-extension-for-transformers.
    user_model = AutoModelForCausalLM.from_pretrained(
        args.model,
        load_in_4bit=args.load_in_4bit,
        load_in_8bit=args.load_in_8bit,
        _commit_hash=args._commit_hash,
        use_neural_speed=args.use_neural_speed,
    )
else:
    print("Didn't do Weight Only Quantization.")

# save model
if args.output_dir is not None and ((args.woq or args.load_in_4bit or args.load_in_8bit) and not args.use_neural_speed):
    user_model.save_pretrained(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    # to validate woq model accuracy 
    args.model = args.output_dir

if args.benchmark:
    print("Loading model from: ", args.model)
    user_model = AutoModelForCausalLM.from_pretrained(
        args.model,
        quantization_config=quantization_config,
        trust_remote_code=args.trust_remote_code,
        _commit_hash=args._commit_hash,
        use_neural_speed=args.use_neural_speed,
    )
    user_model = user_model.eval() if hasattr(user_model, "eval") else user_model
    prompt = "Once upon a time, there existed a little girl, who liked to have adventures. She wanted to go to places and meet new people, and have fun."
    input_size = tokenizer(prompt, return_tensors="pt").input_ids.size(dim=1)
    print("---- Prompt size:", input_size)

    # start
    total_time = 0.0
    num_iter = args.iters
    num_warmup = args.num_warmup
    total_token_num = 0
    eos_token_id = tokenizer.eos_token_id
    with torch.inference_mode(), torch.no_grad():
        for i in range(num_iter):
            tic = time.time()
            # tokenizer for chatglm2.
            if hasattr(tokenizer, "build_chat_input"):
                input_ids = tokenizer.build_chat_input(prompt)["input_ids"]
                input_ids = input_ids.repeat(args.batch_size, 1)
                eos_token_id = [
                    tokenizer.eos_token_id,
                    tokenizer.get_command("<|user|>"),
                    tokenizer.get_command("<|observation|>"),
                ]
            # tokenizer for chatglm3.
            elif hasattr(tokenizer, "build_prompt"):
                build_prompt = tokenizer.build_prompt(prompt)
                input_ids = tokenizer(
                    [build_prompt] * args.batch_size, return_tensors="pt"
                ).input_ids
            else:
                input_ids = tokenizer(
                    [prompt] * args.batch_size, return_tensors="pt"
                ).input_ids
            gen_ids = user_model.generate(
                input_ids,
                max_new_tokens=args.max_new_tokens,
                **generate_kwargs,
                eos_token_id=eos_token_id
            )
            gen_text = tokenizer.batch_decode(gen_ids, skip_special_tokens=True)
            toc = time.time()
            # please check the gen_ids if include input_ids.
            input_tokens_num = input_ids.numel()
            output_tokens_num = torch.tensor(gen_ids).numel() - input_tokens_num
            print(gen_text, flush=True)
            if i >= num_warmup:
                total_time += toc - tic
                total_token_num += output_tokens_num

    print("\n", "-" * 10, "Summary:", "-" * 10)
    latency = total_time / total_token_num
    print("Inference latency: %.3f sec." % latency)
    throughput = total_token_num / total_time
    print("Throughput: {} samples/sec".format(throughput))

if args.accuracy:
    from intel_extension_for_transformers.transformers.llm.evaluation.lm_eval import evaluate, LMEvalParser
    model_args="pretrained="+args.model+",trust_remote_code="+str(args.trust_remote_code)
    if args.use_neural_speed:
        model_args += ",model_format=neural_speed"
    args = LMEvalParser(model = "hf", 
                        model_args=model_args,
                        tasks = args.tasks,
                        device = "cpu",
                        batch_size = args.batch_size)
    results = evaluate(args)
    for task_name in args.tasks.split(","):
        if task_name == "wikitext":
            print("Accuracy for %s is: %s" % (task_name, results["results"][task_name]["word_perplexity,none"]))
        else:
            print("Accuracy for %s is: %s" % (task_name, results["results"][task_name]["acc,none"]))
