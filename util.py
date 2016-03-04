"""Miscellaneous utilities."""
import re

__all__ = ["listify"]

def listify(x):
    """
    Returns [] if x is None, a single-item list consisting of x if x is a str or bytes, otherwise returns x.

    listify(None) -> []
    listify("string") -> ["string"]
    listify(b"bytes") -> [b"bytes"]
    listify(["foo", "bar"]) -> ["foo", "bar"]

    :param x: What to listify.
    :return:
    """
    if x is None:
        return []
    if isinstance(x, (str, bytes)):
        return [x]
    return x
