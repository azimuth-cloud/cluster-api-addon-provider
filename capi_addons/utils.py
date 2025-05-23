import functools
import typing as t


def mergeconcat(defaults, *overrides):
    """
    Returns a new dictionary obtained by deep-merging multiple sets of overrides
    into defaults, with precedence from right to left.
    """

    def mergeconcat2(defaults, overrides):
        if isinstance(defaults, dict) and isinstance(overrides, dict):
            merged = dict(defaults)
            for key, value in overrides.items():
                if key in defaults:
                    merged[key] = mergeconcat2(defaults[key], value)
                else:
                    merged[key] = value
            return merged
        elif isinstance(defaults, list | tuple) and isinstance(overrides, list | tuple):
            merged_list = list(defaults)
            merged_list.extend(overrides)
            return merged_list
        else:
            return overrides if overrides is not None else defaults

    return functools.reduce(mergeconcat2, overrides, defaults)


def check_condition(obj: dict[str, t.Any], name: str) -> bool:
    """
    Returns True if the specified condition exists and is True for the given object,
    False otherwise.
    """
    return any(
        condition["type"] == name and condition["status"] == "True"
        for condition in obj.get("status", {}).get("conditions", [])
    )
