"""
MIT License

Copyright (c) 2020 EleutherAI

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

# Standard
from typing import Any, Dict, List, Union
import abc
import copy
import hashlib
import json
import os

# Third Party
from genai.schema import TextGenerationParameters
from sqlitedict import SqliteDict
from tqdm import tqdm
import transformers

# Local
from fms_sdg.base.generator import BaseGenerator
from fms_sdg.base.instance import Instance
from fms_sdg.utils import sdg_logger

MODEL_ID_OR_PATH = "model_id_or_path"


class LMGenerator(BaseGenerator):
    """Class for LLM Generators"""

    def __init__(self, name: str, config: Dict, **kwargs: Any):
        super().__init__(name, config, **kwargs)
        self._rank = 0
        self.cache_hook = CacheHook(None)

        self.model_id_or_path: str = config.get(MODEL_ID_OR_PATH, None)
        assert (
            self.model_id_or_path is not None
        ), f"Must specify model for Generator {name}"

        default_kwargs = {"decoding_method": "sample"}
        cfg_kwargs = {
            k: v
            for k, v in copy.deepcopy(self.config).items()
            if k in TextGenerationParameters.model_fields
        }
        self._base_kwargs = {**default_kwargs, **cfg_kwargs}

    @property
    def rank(self):
        # used in the case of parallelism. Hardcoded to
        # ensure no errors arise using API models which do
        # not support multi-device parallelism nor expect it.
        return self._rank

    @property
    @abc.abstractmethod
    def eot_token_id(self):
        pass

    @property
    def prefix_token_id(self):
        # it is used as prefix for loglikelihood
        return self.eot_token_id

    @abc.abstractmethod
    def tok_encode(self, string: str, **kwargs):
        pass

    @abc.abstractmethod
    def _loglikelihood_tokens(self, requests, **kwargs):
        pass

    def loglikelihood(
        self, requests: List[Instance], disable_tqdm: bool = False
    ) -> None:
        new_reqs = []
        for req in requests:
            context, continuation = req.args
            if context == "":
                # BOS or EOS as context
                context_enc, continuation_enc = (
                    [self.prefix_token_id],
                    self.tok_encode(continuation),
                )
            else:
                context_enc, continuation_enc = self._encode_pair(context, continuation)

            new_reqs.append((context_enc, continuation_enc, req))

        return self._loglikelihood_tokens(new_reqs, disable_tqdm=disable_tqdm)

    def _encode_pair(self, context, continuation):
        n_spaces = len(context) - len(context.rstrip())
        if n_spaces > 0:
            continuation = context[-n_spaces:] + continuation
            context = context[:-n_spaces]

        model_class = getattr(self, "AUTO_MODEL_CLASS", None)

        if model_class == transformers.AutoModelForSeq2SeqLM:
            context_enc = self.tok_encode(context)
            continuation_enc = self.tok_encode(continuation, add_special_tokens=False)
        else:
            whole_enc = self.tok_encode(context + continuation)
            context_enc = self.tok_encode(context)

            context_enc_len = len(context_enc)
            continuation_enc = whole_enc[context_enc_len:]

        return context_enc, continuation_enc

    def update_instance_with_result(
        self, text: str, instance: Instance, until: List[str]
    ):
        if until is not None:
            for term in until:
                if len(term) > 0:
                    text = text.split(term)[0]
        instance.result = text
        self.cache_hook.add_partial(f"generate_batch", instance, text)

    @abc.abstractmethod
    def generate_batch(
        self, requests: List[Instance], **kwargs: Union[str, Dict]
    ) -> None:
        pass

    def set_cache_hook(self, cache_hook) -> None:
        self.cache_hook = cache_hook


### SQLite-based caching of LM responses
def hash_args(attr, request):
    dat = json.dumps([attr] + [request.args, request.kwargs])
    return hashlib.sha256(dat.encode("utf-8")).hexdigest()


class CacheHook:
    def __init__(self, cachinglm) -> None:
        if cachinglm is None:
            self.dbdict = None
            return

        self.dbdict: SqliteDict = cachinglm.dbdict

    def add_partial(self, attr, req, res) -> None:
        if self.dbdict is None:
            return
        hsh = hash_args(attr, req)
        self.dbdict[hsh] = res


class CachingLM:
    def __init__(self, lm: LMGenerator, cache_db) -> None:
        """LM wrapper that returns cached results if they exist, and uses the underlying LM if not.

        :param lm: LM
            Underlying LM
        :param cache_db: str
            Path to cache db
        """
        self.lm = lm
        self.cache_db = cache_db
        if os.path.dirname(cache_db):
            os.makedirs(os.path.dirname(cache_db), exist_ok=True)
        self.dbdict = SqliteDict(cache_db, autocommit=True)

        # add hook to lm
        lm.set_cache_hook(self.get_cache_hook())

        self.dbdict

    def __getattr__(self, attr):
        lm_attr = getattr(self.lm, attr)
        if not callable(lm_attr):
            return lm_attr

        def fn(requests: List[Instance]):
            res = []
            remaining_reqs = []
            warned = False
            # figure out which ones are cached and which ones are new
            sdg_logger.info(
                f"Loading '{attr}' responses from cache '{self.cache_db}' where possible..."
            )
            for req in tqdm(requests, desc="Checking cached requests"):
                hsh = hash_args(attr, req)
                if (
                    attr == "generate_batch"
                    and req.kwargs.get("decoding_method", None) == "sample"
                ):
                    # when we are doing non-greedy generation, don't use the cache
                    # (else every "randomly sampled" generation would be identical for repeats > 1).
                    if not warned:
                        sdg_logger.warning(
                            f"Arguments to lm.generate_batch() '{req.kwargs}' include non-deterministic sampling. Caching will not be performed for such requests."
                        )
                        warned = True
                    res.append(None)
                    remaining_reqs.append(req)
                elif hsh in self.dbdict:
                    ob = self.dbdict[hsh]
                    assert ob is not None
                    res.append(ob)
                else:
                    res.append(None)
                    remaining_reqs.append(req)

            sdg_logger.info(
                f"Cached requests: {len(requests) - len(remaining_reqs)}, Requests remaining: {len(remaining_reqs)}"
            )

            # actually run the LM on the requests that do not have cached results
            getattr(self.lm, attr)(remaining_reqs)

            # stick the new ones back into the list and also cache any of the new ones
            resptr = 0
            for req in remaining_reqs:
                while res[resptr] is not None:
                    resptr += 1

                res[resptr] = req.result

                # caching
                hsh = hash_args(attr, req)
                self.dbdict[hsh] = req.result
            self.dbdict.commit()

            # now we store result
            for req, req_res in zip(requests, res):
                req.result = req_res

        return fn

    def get_cache_hook(self):
        return CacheHook(self)
