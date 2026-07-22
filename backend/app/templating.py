"""Values-file templating: `${key}` placeholders in manifests are replaced from
`values.yaml` files placed next to (or above) the manifest in the repo tree.

Layering: values files merge from the repo/path root down to the manifest's own
directory — the closest file wins on key conflicts. Nested mappings flatten to
dotted keys (`image: {tag: v1}` -> `${image.tag}`). This gives per-environment
overlays for free: `envs/prod/values.yaml` overrides the root `values.yaml`
for manifests under `envs/prod/`.
"""
import posixpath

VALUES_FILENAMES = ("values.yaml", "values.yml")


def is_values_file(rel_path: str) -> bool:
    return posixpath.basename(rel_path.replace("\\", "/")) in VALUES_FILENAMES


def flatten(values: dict, prefix: str = "") -> dict[str, object]:
    """{"image": {"tag": "v1"}, "count": 2} -> {"image.tag": "v1", "count": 2}"""
    out: dict[str, object] = {}
    for k, v in values.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, f"{key}."))
        else:
            out[key] = v
    return out


def substitute(node, values: dict):
    """Recursively replace ${key} placeholders in strings."""
    if isinstance(node, str):
        for k, v in values.items():
            node = node.replace("${" + k + "}", str(v))
        return node
    if isinstance(node, dict):
        return {k: substitute(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [substitute(v, values) for v in node]
    return node


def values_for(rel_path: str, values_by_dir: dict[str, dict]) -> dict[str, object]:
    """Merge flattened values for a manifest at `rel_path`, root-most first so
    the values file closest to the manifest wins."""
    norm = rel_path.replace("\\", "/")
    parts = norm.split("/")[:-1]   # directories only
    merged: dict[str, object] = {}
    # "" is the root dir key; then walk down each ancestor directory
    for i in range(len(parts) + 1):
        dir_key = "/".join(parts[:i])
        if dir_key in values_by_dir:
            merged.update(values_by_dir[dir_key])
    return merged
