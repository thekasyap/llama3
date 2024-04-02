# Copyright (c) Meta Platforms, Inc. and affiliates.
# This software may be used and distributed according to the terms of the Llama 2 Community License Agreement.

import os
from logging import getLogger
from pathlib import Path
from typing import cast, AbstractSet, Any, Collection, Dict, Iterator, List, Literal, Sequence, Tuple, TypedDict, Union

import tiktoken
from tiktoken.load import load_tiktoken_bpe


logger = getLogger(__name__)


Role = Literal["system", "user", "assistant"]


class Message(TypedDict, total=False):
    role: Role
    content: str


Dialog = Sequence[Message]


class Tokenizer:
    """
    tokenizing and encoding/decoding text using the Tiktoken tokenizer.
    """

    special_tokens: Dict[str, int]

    num_reserved_special_tokens = 256

    pat_str = "(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\\r\\n\\p{L}\\p{N}]?\\p{L}+|\\p{N}{1,3}| ?[^\\s\\p{L}\\p{N}]+[\\r\\n]*|\\s*[\\r\\n]+|\\s+(?!\\S)|\\s+"

    def __init__(self, model_path: str):
        """
        Initializes the Tokenizer with a Tiktoken model.

        Args:
            model_path (str): The path to the Tiktoken model file.
        """
        # reload tokenizer
        assert os.path.isfile(model_path), model_path

        mergeable_ranks = load_tiktoken_bpe(model_path)
        num_base_tokens = len(mergeable_ranks)
        special_tokens = (
            [
                "<|begin_of_text|>",
                "<|end_of_text|>",
                "<|reserved_special_token_0|>",
                "<|reserved_special_token_1|>",
                "<|reserved_special_token_2|>",
                "<|reserved_special_token_3|>",
                "<|start_header_id|>",
                "<|end_header_id|>",
                "<|reserved_special_token_4|>",
                "<|eot_id|>",  # end of turn
            ]
            + [
                f"<|reserved_special_token_{i}|>"
                for i in range(5, self.num_reserved_special_tokens - 5)
            ]
        )
        assert (num_base_tokens + len(special_tokens)) % 8 == 0
        self.special_tokens = {
            token: num_base_tokens + i for i, token in enumerate(special_tokens)
        }
        self.model = tiktoken.Encoding(
            name=Path(model_path).name,
            pat_str=self.pat_str,
            mergeable_ranks=mergeable_ranks,
            special_tokens=self.special_tokens,
        )
        logger.info(f"Reloaded SentencePiece model from {model_path}")

        # BOS / EOS token IDs
        self.n_words: int = self.model.n_vocab
        self.bos_id: int = self.special_tokens["<|begin_of_text|>"]
        self.eos_id: int = self.special_tokens["<|end_of_text|>"]
        self.pad_id: int = -1
        self.stop_tokens = {
            self.special_tokens["<|end_of_text|>"],
            self.special_tokens["<|eot_id|>"],
        }
        logger.info(
            f"#words: {self.n_words} - BOS ID: {self.bos_id} - EOS ID: {self.eos_id}"
        )

    def encode(
        self,
        s: str,
        *,
        bos: bool,
        eos: bool,
        allowed_special: Union[Literal["all"], AbstractSet[str]] = set(),
        disallowed_special: Union[Literal["all"], Collection[str]] = (),
    ) -> List[int]:
        """
        Encodes a string into a list of token IDs.

        Args:
            s (str): The input string to be encoded.
            bos (bool): Whether to prepend the beginning-of-sequence token.
            eos (bool): Whether to append the end-of-sequence token.
            allowed_tokens ("all"|set[str]): allowed special tokens in string
            disallowed_tokens ("all"|set[str]): TODO

        Returns:
            list[int]: A list of token IDs.

        By default, setting disallowed_special=() encodes a string by ignoring
        special tokens. Specifically:
        - Setting `disallowed_special` to () will cause all text corresponding
          to special tokens to be encoded as natural text (insteading of raising
          an error).
        - Setting `allowed_special` to "all" will treat all text corresponding
          to special tokens to be encoded as special tokens.
        """
        assert type(s) is str

        # The tiktoken tokenizer can handle <=400k chars without
        # pyo3_runtime.PanicException (may go beyond 400k)
        TIKTOKEN_MAX_ENCODE_CHARS = 400_000

        # https://github.com/openai/tiktoken/issues/195
        # Here we iterate over subsequences and split if we exceed the limit
        # of max consecutive non-whitespace or whitespace characters.
        MAX_NO_WHITESPACES_CHARS = 25_000

        substrs = (
            substr
            for i in range(0, len(s), TIKTOKEN_MAX_ENCODE_CHARS)
            for substr in self._split_whitespaces_or_nonwhitespaces(
                s[i : i + TIKTOKEN_MAX_ENCODE_CHARS], MAX_NO_WHITESPACES_CHARS
            )
        )
        t: List[int] = []
        for substr in substrs:
            t.extend(
                self.model.encode(
                    substr,
                    allowed_special=allowed_special,
                    disallowed_special=disallowed_special,
                )
            )
        if bos:
            t.insert(0, self.bos_id)
        if eos:
            t.append(self.eos_id)
        return t

    def decode(self, t: Sequence[int]) -> str:
        """
        Decodes a list of token IDs into a string.

        Args:
            t (List[int]): The list of token IDs to be decoded.

        Returns:
            str: The decoded string.
        """
        # typecast is safe here, Tiktoken doesn't do anything list-related with the sequence.
        return self.model.decode(cast(List[int], t))

    @staticmethod
    def _split_whitespaces_or_nonwhitespaces(
        s: str, max_consecutive_slice_len: int
    ) -> Iterator[str]:
        """
        Split the string `s` so that each substring contains no more than `max_consecutive_slice_len`
        consecutive whitespaces or consecutive non-whitespaces
        """
        current_slice_len = 0
        current_slice_is_space = s[0].isspace() if len(s) > 0 else False
        slice_start = 0

        for i in range(len(s)):
            is_now_space = s[i].isspace()

            if current_slice_is_space ^ is_now_space:
                current_slice_len = 1
                current_slice_is_space = is_now_space
            else:
                current_slice_len += 1
                if current_slice_len > max_consecutive_slice_len:
                    yield s[slice_start:i]
                    slice_start = i
                    current_slice_len = 1
        yield s[slice_start:]


class ParseError(ValueError):
    pass


class MessageFormat:
    def __init__(self, tokenizer: Tokenizer):
        self.tokenizer = tokenizer

    def encode_header(self, message: Message) -> List[int]:
        tokens = []
        tokens.append(self.tokenizer.special_tokens["<|start_header_id|>"])
        tokens.extend(self.tokenizer.encode(message["role"], bos=False, eos=False))
        tokens.append(self.tokenizer.special_tokens["<|end_header_id|>"])
        tokens.extend(self.tokenizer.encode("\n\n", bos=False, eos=False))
        return tokens

    def encode_message(self, message: Message) -> List[int]:
        tokens = self.encode_header(message)
        if message.get("content", ""):
            tokens.extend(self.tokenizer.encode(message["content"].strip(), bos=False, eos=False))
        tokens.append(self.tokenizer.special_tokens["<|eot_id|>"])
        return tokens

    def encode_dialog(self, dialog: Dialog, *, bos: bool, eos: bool) -> List[int]:
        tokens = []
        if bos:
            tokens.append(self.tokenizer.special_tokens["<|begin_of_text|>"])
        for message in dialog:
            tokens.extend(self.encode_message(message))

        if eos:
            # Add EOS token at the end of this dialog if required
            tokens.append(self.tokenizer.special_tokens["<|end_of_text|>"])
        elif dialog[-1]["role"] == "assistant":
            # Remove <|eot_id|> if the last turn is from Assistant to allow completion
            tokens.pop()
        return tokens

    def decode_header(self, tokens: Sequence[int]) -> Tuple[Sequence[int], Message]:
        tokens, _ = self._take(tokens, "<|start_header_id|>")
        tokens, tokens_role, _ = self._take_until(tokens, "<|end_header_id|>")
        tokens, _ = self._take(tokens, "\n\n")
        role = self.tokenizer.decode(tokens_role)
        return tokens, {"role": cast(Role, role)}  # TODO: check if valid role?

    def decode_message(self, tokens: Sequence[int]) -> Tuple[Sequence[int], Message]:
        tokens, message = self.decode_header(tokens)
        tokens, tokens_content, _ = self._take_until(tokens, "<|eot_id|>")
        message["content"] = self.tokenizer.decode(tokens_content)
        return tokens, message

    def _take(self, tokens: Sequence[int], *expected_strs:str) -> Tuple[Sequence[int], Sequence[int]]:
        for expected_str in expected_strs:
            t = self.tokenizer.encode(expected_str, bos=False, eos=False, allowed_special="all")
            if len(tokens) < len(t):
                continue
            if tokens[:len(t)] != t:
                continue
            return tokens[len(t):], tokens[:len(t)]
        raise ParseError(f"Expected any of {expected_strs!r}")

    def _take_until(self, tokens: Sequence[int], *expected_strs:str) -> Tuple[Sequence[int], Sequence[int], Sequence[int]]:
        best = None
        for expected_str in expected_strs:
            t = self.tokenizer.encode(expected_str, bos=False, eos=False, allowed_special="all")
            if len(tokens) < len(t):
                continue

            offset = 0
            try:
                while offset < len(tokens):
                    offset = tokens.index(t[0], offset)
                    if tokens[offset:offset + len(t)] == t:
                        if best is None or offset < best[0]:
                            best = (offset, t)
                        break
            except ValueError:
                continue
        if best is not None:
            return (
                tokens[best[0] + len(best[1]):],  # next tokens
                tokens[:best[0]],  # tokens up to found sequence,
                tokens[best[0]: best[0] + len(best[1])],  # found sequence itself
            )
        raise ParseError(f"Expected tokens followed by any of {expected_strs!r}")
