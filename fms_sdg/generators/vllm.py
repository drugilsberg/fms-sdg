"""
MIT License

Copyright (c) 2020 EleutherAI

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

# Standard
from importlib.metadata import version
from importlib.util import find_spec
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
import copy

# Third Party
from more_itertools import distribute
from packaging.version import parse as parse_version
from tqdm import tqdm

# Local
from fms_sdg.base.instance import Instance
from fms_sdg.base.registry import register_generator
from fms_sdg.generators.llm import LMGenerator
from fms_sdg.generators.utils import Collator, undistribute
from fms_sdg.utils import sdg_logger
import fms_sdg.generators.utils as generator_utils

try:
    # Third Party
    from vllm import LLM
    from vllm.sampling_params import SamplingParams
    from vllm.transformers_utils.tokenizer import get_tokenizer
    from vllm.utils import random_uuid
    import ray
except ModuleNotFoundError:
    pass

sdg_logger = sdg_logger


# TODO: this can be made more efficient for our purposes by rewriting the async code ourselves
@register_generator("vllm")
class vLLMGenerator(LMGenerator):
    """vLLM Generator"""

    _DEFAULT_MAX_LENGTH = 2048

    def __init__(self, name: str, config: Dict, **kwargs: Any):
        super().__init__(name, config, **kwargs)

        if not find_spec("vllm"):
            raise Exception(
                "attempted to use 'vllm' LM type, but package `vllm` is not installed. "
                "Please install vllm via `pip install fms_sdg[vllm]`"
            )

        pretrained = self._config.get(
            "model_id_or_path",
            None,
        )
        dtype: Literal["float16", "bfloat16", "float32", "auto"] = self._config.get(
            "dtype", "auto"
        )
        revision: Optional[str] = self._config.get("revision", None)
        trust_remote_code: Optional[bool] = self._config.get("trust_remote_code", False)
        tokenizer: Optional[str] = self._config.get("tokenizer", None)
        tokenizer_mode: Literal["auto", "slow"] = self._config.get(
            "tokenizer_mode", "auto"
        )
        tokenizer_revision: Optional[str] = self._config.get("tokenizer_revision", None)
        add_bos_token: Optional[bool] = self._config.get("add_bos_token", False)
        prefix_token_id: Optional[int] = self._config.get("prefix_token_id", None)
        tensor_parallel_size: int = self._config.get("tensor_parallel_size", 1)
        quantization: Optional[str] = self._config.get("quantization", None)
        max_gen_toks: int = self._config.get("max_gen_toks", 256)
        swap_space: int = self._config.get("swap_space", 4)
        batch_size: Union[str, int] = self._config.get("batch_size", "auto")
        max_batch_size = self._config.get("max_batch_size", None)
        max_length: int = self._config.get("max_length", None)
        max_model_len: int = self._config.get("max_model_len", None)
        seed: int = self._config.get("seed", 1234)
        gpu_memory_utilization: float = self._config.get("gpu_memory_utilization", 0.9)
        device: str = self._config.get("device", "cuda")
        data_parallel_size: int = self._config.get("data_parallel_size", 1)

        assert "cuda" in device or device is None, "vLLM only supports CUDA"
        assert (
            max_length is None or max_model_len is None
        ), "Either max_length or max_model_len may be provided, but not both"

        self._max_length = max_model_len if max_model_len is not None else max_length
        self.tensor_parallel_size = int(tensor_parallel_size)
        self.data_parallel_size = int(data_parallel_size)
        self.model_args = {
            "model": pretrained,
            "gpu_memory_utilization": float(gpu_memory_utilization),
            "revision": revision,
            "dtype": dtype,
            "tokenizer": tokenizer,
            "tokenizer_mode": tokenizer_mode,
            "tokenizer_revision": tokenizer_revision,
            "trust_remote_code": trust_remote_code,
            "tensor_parallel_size": int(tensor_parallel_size),
            "max_model_len": int(self._max_length) if self._max_length else None,
            "swap_space": int(swap_space),
            "quantization": quantization,
            "seed": int(seed),
        }

        self.batch_size = (
            "auto"
            if isinstance(batch_size, str) and "auto" in batch_size
            else batch_size
        )
        if self.data_parallel_size <= 1:
            self.model = LLM(**self.model_args)
        else:
            assert parse_version(version("vllm")) < parse_version(
                "0.3.3"
            ), "data_parallel is only compatible with vllm < v0.3.3."
            sdg_logger.warning(
                "You might experience occasional issues with model weight downloading when data_parallel is in use. To ensure stable performance, run with data_parallel_size=1 until the weights are downloaded and cached."
            )
            self.model_args["worker_use_ray"] = True
            self.batch_size = "auto"
            sdg_logger.info("Manual batching is not compatible with data parallelism.")

            # Third Party
            from transformers import AutoConfig

            self._config = AutoConfig.from_pretrained(
                pretrained, trust_remote_code=trust_remote_code, revision=revision
            )
        self.tokenizer = get_tokenizer(
            tokenizer if tokenizer else pretrained,
            tokenizer_mode=tokenizer_mode,
            trust_remote_code=trust_remote_code,
            tokenizer_revision=tokenizer_revision,
        )
        self.add_bos_token = add_bos_token
        self.custom_prefix_token_id = prefix_token_id
        if prefix_token_id is not None:
            sdg_logger.info(
                f"Loglikelihood prefix token id used in evaluation: {self.prefix_token_id}"
            )

        self._max_gen_toks = max_gen_toks

    @property
    def eot_token_id(self):
        # we use EOT because end of *text* is more accurate for what we're doing than end of *sentence*
        return self.tokenizer.eos_token_id

    @property
    def prefix_token_id(self):
        # it is used as prefix for loglikelihood
        if self.custom_prefix_token_id is not None:
            return self.custom_prefix_token_id
        if self.tokenizer.bos_token_id is not None:
            return self.tokenizer.bos_token_id
        return self.tokenizer.eos_token_id

    @property
    def max_length(self):
        if self._max_length:  # if max length manually set, return it
            return self._max_length
        if self.data_parallel_size <= 1:
            return self.model.llm_engine.model_config.max_model_len
        else:
            seqlen_config_attrs = ("n_positions", "max_position_embeddings", "n_ctx")
            for attr in seqlen_config_attrs:
                if hasattr(self._config, attr):
                    return getattr(self._config, attr)
            if hasattr(self.tokenizer, "model_max_length"):
                if self.tokenizer.model_max_length == 1000000000000000019884624838656:
                    return self._DEFAULT_MAX_LENGTH
                return self.tokenizer.model_max_length
            return self._DEFAULT_MAX_LENGTH

    @property
    def max_gen_toks(self):
        return self._max_gen_toks

    def tok_encode(
        self,
        string: str,
        left_truncate_len=None,
        add_special_tokens=None,
        truncation=False,
    ):
        """ """
        if not add_special_tokens:
            add_special_tokens = False or self.add_bos_token
        encoding = self.tokenizer.encode(
            string, add_special_tokens=add_special_tokens, truncation=truncation
        )

        # left-truncate the encoded context to be at most `left_truncate_len` tokens long
        if left_truncate_len:
            encoding = encoding[-left_truncate_len:]

        return encoding

    def _model_generate(
        self,
        requests: List[List[int]] = None,
        generate: bool = False,
        stop: Optional[List[str]] = None,
        **kwargs,
    ):
        if generate:
            kwargs = self.modify_gen_kwargs(kwargs)
            sampling_params = SamplingParams(stop=stop, **kwargs)
        else:
            sampling_params = SamplingParams(
                temperature=0, prompt_logprobs=1, max_tokens=1
            )
        if self.data_parallel_size > 1:
            # vLLM hangs if tensor_parallel > 1 and resources are set in ray.remote
            # also seems to only work with decorator and not with ray.remote() fn
            # see https://github.com/vllm-project/vllm/issues/973
            # note: this has changed on 0.3.3, and it only works now if num_gpus are set.
            # but then tensor_parallel breaks
            @ray.remote
            def run_inference_one_model(
                model_args: dict, sampling_params, requests: List[List[int]]
            ):
                llm = LLM(**model_args)
                return llm.generate(
                    prompt_token_ids=requests, sampling_params=sampling_params
                )

            # dispatch requests to all self.data_parallel_size workers, in interleaved fashion
            # interleaved important to balance context lengths across workers
            requests = [list(x) for x in distribute(self.data_parallel_size, requests)]
            inputs = ((self.model_args, sampling_params, req) for req in requests)
            object_refs = [run_inference_one_model.remote(*x) for x in inputs]
            results = ray.get(object_refs)
            # Invoke ray.shutdown() to prevent hang-ups if subsequent calls required.
            ray.shutdown()
            # flatten results
            return undistribute(results)

        outputs = self.model.generate(
            prompt_token_ids=requests,
            sampling_params=sampling_params,
            use_tqdm=True if self.batch_size == "auto" else False,
        )
        return outputs

    def generate_batch(
        self, requests: List[Instance], disable_tqdm: bool = False
    ) -> None:
        # batch tokenize contexts
        context = [req.args[0] for req in requests]
        context_encoding = self.tokenizer(context, add_special_tokens=False).input_ids
        request_list = [
            ((a, b), c) for a, b, c in zip(context, context_encoding, requests)
        ]

        grouper = generator_utils.Grouper(request_list, lambda x: str(x[1].kwargs))
        pbar = tqdm(
            total=len(request_list),
            disable=(disable_tqdm or (self.rank != 0)),
            desc="Running generate_batch requests",
        )

        for key, c_ce_reqs in grouper.get_grouped().items():

            chunks = generator_utils.chunks(
                c_ce_reqs,
                n=int(self.batch_size) if self.batch_size != "auto" else 0,
            )

            for chunk in chunks:
                context_and_encoding, chunk_instances = zip(*chunk)
                context, context_encoding = zip(*context_and_encoding)
                # all kwargs are identical within a chunk
                gen_kwargs = next(iter(chunk_instances)).kwargs

                # unpack our keyword arguments.
                until = None
                if isinstance(kwargs := copy.deepcopy(gen_kwargs), dict):
                    # start with default params in self.config then overwrite with kwargs
                    kwargs = {**self._base_kwargs, **kwargs}
                    if "stop_sequences" in kwargs:
                        until = kwargs.pop("stop_sequences")
                        if isinstance(until, str):
                            until = [until]
                        elif not isinstance(until, list):
                            raise ValueError(
                                f"Expected `kwargs['stop_sequences']` to be of type Union[str,list] but got {until}"
                            )
                    kwargs["max_tokens"] = self.max_gen_toks
                    if "max_new_tokens" in kwargs.keys():
                        kwargs["max_tokens"] = kwargs.pop("max_new_tokens")
                    if "min_new_tokens" in kwargs:
                        kwargs["min_tokens"] = kwargs.pop("min_new_tokens")
                    if "decoding_method" in kwargs:
                        kwargs["do_sample"] = kwargs.pop("decoding_method") == "sample"
                else:
                    raise ValueError(
                        f"Expected `kwargs` to be of type `dict` but got {gen_kwargs}"
                    )
                # add EOS token to stop sequences
                eos = self.tokenizer.decode(self.eot_token_id)
                if not until:
                    until = [eos]
                else:
                    until.append(eos)

                # set the max length in tokens of inputs ("context_enc")
                # max len for inputs = max length, minus room to generate the max new tokens
                max_ctx_len = self.max_length - kwargs["max_tokens"]
                context_encoding = [x[-max_ctx_len:] for x in context_encoding]

                # perform batched generation
                cont = self._model_generate(
                    requests=context_encoding,
                    generate=True,
                    stop=until,
                    **kwargs,
                )

                for output, instance in zip(cont, chunk_instances):
                    s = output.outputs[0].text
                    self.update_instance_with_result(s, instance, until)
                    pbar.update(1)

        pbar.close()

    def _loglikelihood_tokens(
        self,
        requests: List[Tuple[Tuple[str, str], List[int], List[int]]],
        disable_tqdm: bool = False,
    ) -> List[float]:
        res = []

        def _collate(x):
            toks = x[1] + x[2]
            return -len(toks), tuple(toks)

        # Reorder requests by length and batch
        re_ord = Collator(requests, sort_fn=_collate)
        chunks = re_ord.get_batched(
            n=int(self.batch_size) if self.batch_size != "auto" else 0, batch_fn=None
        )

        pbar = tqdm(
            total=len(requests),
            disable=disable_tqdm,
            desc="Running loglikelihood requests",
        )
        for chunk in chunks:
            inputs = []
            ctxlens = []
            for cache_key, context_enc, continuation_enc in chunk:
                inp = (context_enc + continuation_enc)[-(self.max_length) :]
                ctxlen = len(context_enc) - max(
                    0, len(context_enc) + len(continuation_enc) - (self.max_length)
                )

                inputs.append(inp)
                ctxlens.append(ctxlen)

            outputs = self._model_generate(requests=inputs, generate=False)

            for output, ctxlen, (cache_key, _, _), inp in zip(
                outputs, ctxlens, chunk, inputs
            ):
                answer = self._parse_logprobs(
                    tokens=inp,
                    outputs=output,
                    ctxlen=ctxlen,
                )

                res.append(answer)

                pbar.update(1)

        pbar.close()
        return re_ord.get_original(res)

    @staticmethod
    def _parse_logprobs(tokens: List, outputs, ctxlen: int) -> Tuple[float, bool]:
        """Process logprobs and tokens.

        :param tokens: list
            Input tokens (potentially left-truncated)
        :param outputs: RequestOutput
            Contains prompt_logprobs
        :param ctxlen: int
            Length of context (so we can slice them away and only keep the predictions)
        :return:
            continuation_logprobs: float
                Log probabilities of continuation tokens
            is_greedy: bool
                Whether argmax matches given continuation exactly
        """

        # The first entry of prompt_logprobs is None because the model has no previous tokens to condition on.
        continuation_logprobs_dicts = outputs.prompt_logprobs

        def coerce_logprob_to_num(logprob):
            # vLLM changed the return type of logprobs from float
            # to a Logprob object storing the float value + extra data
            # (https://github.com/vllm-project/vllm/pull/3065).
            # If we are dealing with vllm's Logprob object, return
            # the logprob value stored as an attribute. Otherwise,
            # return the object itself (which should be a float
            # for older versions of vLLM).
            return getattr(logprob, "logprob", logprob)

        continuation_logprobs_dicts = [
            (
                {
                    token: coerce_logprob_to_num(logprob)
                    for token, logprob in logprob_dict.items()
                }
                if logprob_dict is not None
                else None
            )
            for logprob_dict in continuation_logprobs_dicts
        ]

        # Calculate continuation_logprobs
        # assume ctxlen always >= 1
        continuation_logprobs = sum(
            logprob_dict.get(token)
            for token, logprob_dict in zip(
                tokens[ctxlen:], continuation_logprobs_dicts[ctxlen:]
            )
        )

        # # Determine if is_greedy
        # is_greedy = True
        # for token, logprob_dict in zip(
        #     tokens[ctxlen:], continuation_logprobs_dicts[ctxlen:]
        # ):
        #     # Get the token with the maximum log probability from the logprob_dict
        #     if logprob_dict:  # Ensure the logprob_dict is not None
        #         top_token = max(logprob_dict, key=logprob_dict.get)
        #         if top_token != token:
        #             is_greedy = False
        #             break

        # return continuation_logprobs, is_greedy
        return continuation_logprobs

    @staticmethod
    def modify_gen_kwargs(kwargs: dict) -> dict:
        # sampling_params
        do_sample = kwargs.pop("do_sample", None)
        if do_sample is False or "temperature" not in kwargs:
            kwargs["temperature"] = 0.0
        # hf defaults
        kwargs["skip_special_tokens"] = kwargs.get("skip_special_tokens", False)
        kwargs["spaces_between_special_tokens"] = kwargs.get(
            "spaces_between_special_tokens", False
        )
        return kwargs
