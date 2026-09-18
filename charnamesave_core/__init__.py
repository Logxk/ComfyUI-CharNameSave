"""ComfyUI-CharNameSave core package.

Aggregates the internal API (including the historically underscore-prefixed
names) so the top-level ``__init__`` can re-export everything unchanged.
"""

from .constants import (  # noqa: F401
    _BARE_NAME_MIN_LEN,
    _BARE_NAME_MODE_EXACT,
    _BARE_NAME_MODE_FUZZY,
    _BARE_NAME_MODE_OFF,
    _BARE_NAME_MODES,
    _BARE_NAME_STOPWORDS,
    _DATASET_PATH,
    _DEFAULT_MODE,
    _FUZZY_CUTOFF,
    _FUZZY_MAX_LEN_DIFF,
    _MAX_AUTO_NAMES,
    _MODE_ALIASES,
    _MODES,
    _MULTI_GROUP_TAGS,
    _MULTI_NAME_MAX_LEN,
)
from .dataset import (  # noqa: F401
    _CHARACTER_INDEX,
    _CHARACTER_NAMES_LOWER,
    _character_output_name,
    _dataset_key,
    _load_character_index,
)
from .textparse import (  # noqa: F401
    _clean_path_part,
    _is_artist_tag,
    _iter_tags,
    _prompt_texts,
    _sanitize_name,
    _series_name_text,
    _strip_weights,
    _unescape_tag,
)
from .matching import (  # noqa: F401
    _auto_candidates,
    _candidate_from_tag,
    _character_bare_name,
    _character_name_from_tag,
    _dataset_lookup,
    _dataset_short_name,
    _fuzzy_candidate_matches,
    _looks_like_character_name,
    _meta_series_name,
    _meta_series_of,
    _output_name_for_record,
    _resolve_character_name,
)
from .naming import (  # noqa: F401
    _build_save_prefix,
    _char_names,
    _dedupe_character_names,
    _group_tag_for_count,
    _join_character_names,
    _sorted_character_names,
)
from .node import (  # noqa: F401
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    CharNameSaveImage,
    folder_paths,
)
from .selftest import _run_self_test, _self_test  # noqa: F401

__all__ = [
    # constants
    "_BARE_NAME_MIN_LEN",
    "_BARE_NAME_MODE_EXACT",
    "_BARE_NAME_MODE_FUZZY",
    "_BARE_NAME_MODE_OFF",
    "_BARE_NAME_MODES",
    "_BARE_NAME_STOPWORDS",
    "_DATASET_PATH",
    "_DEFAULT_MODE",
    "_FUZZY_CUTOFF",
    "_FUZZY_MAX_LEN_DIFF",
    "_MAX_AUTO_NAMES",
    "_MODE_ALIASES",
    "_MODES",
    "_MULTI_GROUP_TAGS",
    "_MULTI_NAME_MAX_LEN",
    # dataset
    "_CHARACTER_INDEX",
    "_CHARACTER_NAMES_LOWER",
    "_character_output_name",
    "_dataset_key",
    "_load_character_index",
    # textparse
    "_clean_path_part",
    "_is_artist_tag",
    "_iter_tags",
    "_prompt_texts",
    "_sanitize_name",
    "_series_name_text",
    "_strip_weights",
    "_unescape_tag",
    # matching
    "_auto_candidates",
    "_candidate_from_tag",
    "_character_bare_name",
    "_character_name_from_tag",
    "_dataset_lookup",
    "_dataset_short_name",
    "_fuzzy_candidate_matches",
    "_looks_like_character_name",
    "_meta_series_name",
    "_meta_series_of",
    "_output_name_for_record",
    "_resolve_character_name",
    # naming
    "_build_save_prefix",
    "_char_names",
    "_dedupe_character_names",
    "_group_tag_for_count",
    "_join_character_names",
    "_sorted_character_names",
    # node
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "CharNameSaveImage",
    "folder_paths",
    # selftest
    "_run_self_test",
    "_self_test",
]
