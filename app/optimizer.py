"""
TinyAnim — Core Optimization Engine
====================================

Two independent, dependency-light optimizers:

* ``LottieOptimizer``  — structural compression of Lottie (Bodymovin) JSON.
* ``SVGOptimizer``     — path / metadata compression of SVG markup.

Both are designed to be *visually lossless*: the only information that is
discarded is precision the human eye cannot perceive (excess float digits)
and authoring metadata that has no effect on rendering (layer names, editor
namespaces, comments, etc.).

The module has **no third-party dependencies** so it can be unit-tested and
reused in isolation from the web layer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _round_number(value: float, precision: int) -> float | int:
    """Round ``value`` to ``precision`` decimals, collapsing ``x.0`` to ``int``.

    Collapsing to ``int`` shaves the trailing ``.0`` from the serialized JSON,
    which adds up across thousands of keyframe values.
    """
    rounded = round(value, precision)
    if rounded == int(rounded):
        return int(rounded)
    return rounded


@dataclass(slots=True)
class OptimizationResult:
    """Outcome of a single optimization pass."""

    data: bytes
    original_size: int
    optimized_size: int

    @property
    def saved_bytes(self) -> int:
        return max(self.original_size - self.optimized_size, 0)

    @property
    def reduction_ratio(self) -> float:
        """Reduction as a fraction in ``[0, 1]``."""
        if self.original_size == 0:
            return 0.0
        return self.saved_bytes / self.original_size

    @property
    def reduction_percent(self) -> float:
        return round(self.reduction_ratio * 100, 1)


# --------------------------------------------------------------------------- #
# Lottie (JSON) optimizer
# --------------------------------------------------------------------------- #
class LottieOptimizer:
    """Structurally compress a Lottie animation without touching its visuals.

    Strategy
    --------
    1. Recursively round every float to ``precision`` decimals. Coordinates,
       bezier tangents and time values carry far more precision than any
       display can resolve.
    2. Strip authoring metadata that the player ignores: layer/shape ``nm``
       names, the ``mn`` match-name, and the top-level ``meta`` block (author,
       generator, description, keywords).
    3. Re-serialize with the most compact JSON separators (no spaces).
    """

    #: Keys whose *values* are pure authoring metadata and safe to drop.
    _METADATA_KEYS = frozenset({"nm", "mn"})

    def __init__(self, precision: int = 3, strip_names: bool = True) -> None:
        if not 0 <= precision <= 8:
            raise ValueError("precision must be between 0 and 8")
        self.precision = precision
        self.strip_names = strip_names

    # -- public API ------------------------------------------------------- #
    def optimize(self, raw: bytes) -> OptimizationResult:
        original_size = len(raw)

        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"Invalid Lottie JSON: {exc}") from exc

        if not isinstance(document, dict):
            raise ValueError("Lottie root must be a JSON object")

        cleaned = self._transform(document)

        # Compact separators are the single biggest textual win.
        optimized = json.dumps(
            cleaned, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

        return OptimizationResult(
            data=optimized,
            original_size=original_size,
            optimized_size=len(optimized),
        )

    # -- internals -------------------------------------------------------- #
    def _transform(self, node: Any) -> Any:
        """Depth-first walk that rounds numbers and prunes metadata."""
        if isinstance(node, dict):
            result: dict[str, Any] = {}
            for key, value in node.items():
                # Drop the top-level meta block entirely — it never renders.
                if key == "meta":
                    continue
                if self.strip_names and key in self._METADATA_KEYS:
                    continue
                result[key] = self._transform(value)
            return result

        if isinstance(node, list):
            return [self._transform(item) for item in node]

        if isinstance(node, bool):
            # bool is a subclass of int — keep it as-is, never round.
            return node

        if isinstance(node, float):
            return _round_number(node, self.precision)

        return node


# --------------------------------------------------------------------------- #
# SVG optimizer
# --------------------------------------------------------------------------- #
class SVGOptimizer:
    """Compress SVG markup by removing editor cruft and shrinking numbers.

    Uses regex-based transforms rather than a full XML DOM so that we never
    re-order attributes or restructure the tree (which can subtly change
    rendering). Each transform is conservative and visual-safe.
    """

    # XML comments  <!-- ... -->
    _RE_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
    # <?xml ... ?> processing instructions / declarations
    _RE_PI = re.compile(r"<\?.*?\?>", re.DOTALL)
    # <!DOCTYPE ...>
    _RE_DOCTYPE = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE)
    # Editor-specific namespaced attributes, e.g. sketch:type, inkscape:label
    _RE_EDITOR_NS = re.compile(
        r"\s+(?:sketch|inkscape|sodipodi|illustrator|graph|i|x|adobe|figma)"
        r":[\w-]+\s*=\s*([\"']).*?\1",
        re.IGNORECASE,
    )
    # Editor-specific namespace declarations on the root element
    _RE_EDITOR_NS_DECL = re.compile(
        r"\s+xmlns:(?:sketch|inkscape|sodipodi|illustrator|graph|i|x|adobe|figma)"
        r"\s*=\s*([\"']).*?\1",
        re.IGNORECASE,
    )
    # xml:space="preserve"
    _RE_XML_SPACE = re.compile(r'\s+xml:space\s*=\s*(["\']).*?\1', re.IGNORECASE)
    # id="..." attributes (referenced ids are restored afterwards)
    _RE_ID_ATTR = re.compile(r'\s+id\s*=\s*(["\'])(.*?)\1', re.IGNORECASE)
    # data-* authoring attributes
    _RE_DATA_ATTR = re.compile(r'\s+data-[\w-]+\s*=\s*(["\']).*?\1', re.IGNORECASE)
    # Whitespace between tags
    _RE_INTERTAG_WS = re.compile(r">\s+<")
    # Numbers in attribute/path payloads
    _RE_NUMBER = re.compile(r"-?\d*\.\d+(?:[eE][-+]?\d+)?|-?\d+(?:[eE][-+]?\d+)?")
    # url(#id) / href="#id" references — ids that must be preserved
    _RE_REF = re.compile(r"(?:url\(\s*#|href\s*=\s*[\"']#|xlink:href\s*=\s*[\"']#)([\w:.-]+)")

    def __init__(self, precision: int = 2, strip_ids: bool = True) -> None:
        if not 0 <= precision <= 8:
            raise ValueError("precision must be between 0 and 8")
        self.precision = precision
        self.strip_ids = strip_ids

    # -- public API ------------------------------------------------------- #
    def optimize(self, raw: bytes) -> OptimizationResult:
        original_size = len(raw)

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")

        if "<svg" not in text.lower():
            raise ValueError("File does not contain an <svg> root element")

        # 1. Drop comments, declarations, doctype.
        text = self._RE_COMMENT.sub("", text)
        text = self._RE_PI.sub("", text)
        text = self._RE_DOCTYPE.sub("", text)

        # 2. Drop editor metadata / namespaces.
        text = self._RE_EDITOR_NS_DECL.sub("", text)
        text = self._RE_EDITOR_NS.sub("", text)
        text = self._RE_XML_SPACE.sub("", text)
        text = self._RE_DATA_ATTR.sub("", text)

        # 3. Strip ids that are never referenced.
        if self.strip_ids:
            text = self._strip_unreferenced_ids(text)

        # 4. Round numbers inside coordinate-bearing attributes.
        text = self._round_path_data(text)
        text = self._round_geometry_attrs(text)

        # 5. Collapse inter-tag and redundant whitespace.
        text = self._RE_INTERTAG_WS.sub("><", text)
        text = re.sub(r"\s{2,}", " ", text)
        text = text.strip()

        optimized = text.encode("utf-8")
        return OptimizationResult(
            data=optimized,
            original_size=original_size,
            optimized_size=len(optimized),
        )

    # -- internals -------------------------------------------------------- #
    def _strip_unreferenced_ids(self, text: str) -> str:
        """Remove ``id`` attributes that nothing in the document points to."""
        referenced = set(self._RE_REF.findall(text))

        def replace(match: re.Match[str]) -> str:
            ident = match.group(2)
            return match.group(0) if ident in referenced else ""

        return self._RE_ID_ATTR.sub(replace, text)

    def _round_number_token(self, match: re.Match[str]) -> str:
        token = match.group(0)
        try:
            value = float(token)
        except ValueError:
            return token
        rounded = _round_number(value, self.precision)
        return repr(rounded) if isinstance(rounded, float) else str(rounded)

    def _round_path_data(self, text: str) -> str:
        """Round numbers and trim whitespace inside ``d`` path attributes."""

        def process_d(match: re.Match[str]) -> str:
            quote = match.group(1)
            payload = match.group(2)
            payload = self._RE_NUMBER.sub(self._round_number_token, payload)
            # Collapse whitespace; drop spaces around command letters & commas.
            payload = re.sub(r"\s+", " ", payload).strip()
            payload = re.sub(r"\s*,\s*", ",", payload)
            payload = re.sub(r"\s*([MmLlHhVvCcSsQqTtAaZz])\s*", r"\1", payload)
            # A space before a negative number is redundant.
            payload = payload.replace(" -", "-")
            return f"d={quote}{payload}{quote}"

        return re.sub(
            r'd\s*=\s*(["\'])(.*?)\1', process_d, text, flags=re.DOTALL
        )

    def _round_geometry_attrs(self, text: str) -> str:
        """Round numbers inside geometry / transform attributes."""
        attrs = (
            "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
            "width", "height", "points", "transform", "offset",
            "stroke-width", "viewBox", "gradientTransform", "fx", "fy",
        )
        pattern = re.compile(
            r"\b(" + "|".join(re.escape(a) for a in attrs) + r')\s*=\s*(["\'])(.*?)\2',
            re.DOTALL,
        )

        def process(match: re.Match[str]) -> str:
            name, quote, payload = match.group(1), match.group(2), match.group(3)
            payload = self._RE_NUMBER.sub(self._round_number_token, payload)
            payload = re.sub(r"\s{2,}", " ", payload).strip()
            return f"{name}={quote}{payload}{quote}"

        return pattern.sub(process, text)


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def optimize_file(raw: bytes, kind: str, *, precision: int | None = None) -> OptimizationResult:
    """Optimize ``raw`` bytes for the given ``kind`` (``"lottie"`` | ``"svg"``)."""
    if kind == "lottie":
        opt = LottieOptimizer(precision=precision if precision is not None else 3)
        return opt.optimize(raw)
    if kind == "svg":
        opt = SVGOptimizer(precision=precision if precision is not None else 2)
        return opt.optimize(raw)
    raise ValueError(f"Unsupported file kind: {kind!r}")
