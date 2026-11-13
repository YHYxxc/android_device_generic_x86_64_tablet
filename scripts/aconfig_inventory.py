#!/usr/bin/env python3
"""Inventory the aconfig flags in an AOSP/Lineage tree, with their release values.

  device/generic/x86_64_tablet/scripts/aconfig_inventory.py scan <aosp-root> <release> [-o flags.csv]
  device/generic/x86_64_tablet/scripts/aconfig_inventory.py diff <old.csv> <new.csv> [--namespaces a,b] [-o diff.md]
  device/generic/x86_64_tablet/scripts/aconfig_inventory.py rw-pack <flags.csv> <release> <out-dir>
                             (--namespaces a,b | --namespaces-file conf) [--prefix bass-rw]
  device/generic/x86_64_tablet/scripts/aconfig_inventory.py caches <aosp-root> <release> <out-dir>
                             (--namespaces a,b | --namespaces-file conf) [--maps-file f] [--map m]

scan reads every *.aconfig declaration (out/ and .repo/ skipped) and resolves each flag's
value for <release> (cp2a, bp4a, ...) by walking the release's `inherits` chain through the
release maps build/make/core/release_config.mk always loads (build/release, then
device/*/release, device/*/*/release, vendor/*/release, vendor/*/*/release; parents first,
later values win). Flags no release sets keep their declared default (DISABLED).
--exclude-root drops a map directory from the scan (rw-pack's own output, for example).

diff lists flags added, removed, and changed (value, read-only, namespace) between two
scans, optionally limited to some namespaces.

rw-pack writes aconfig values that keep each flag's resolved state but make it READ_WRITE,
for flags in the given namespaces that build/release set READ_ONLY and that are not
is_fixed_read_only. Flags another map sets (Lineage, addons) are left to that map. Flags no
map sets need no entry: RELEASE_ACONFIG_FLAG_DEFAULT_PERMISSION covers them. The output is
an Android.bp (one aconfig_values per package plus an aconfig_value_set named
aconfig_value_set-<prefix>-<release>) and <package>/<prefix>_flag_values.textproto files.

caches writes one aconfig cache (<package>.pb, the parsed_flags format of aconfig_flags.pb)
per package with flags in the given namespaces, using the host aconfig tool the way the
build does: the release's aconfig_value_sets in order (each map's release_configs/, the
modules they name in its Android.bp files), its RELEASE_ACONFIG_FLAG_DEFAULT_PERMISSION and
RELEASE_ACONFIG_REQUIRE_ALL_READ_ONLY. Product maps (PRODUCT_RELEASE_CONFIG_MAPS) come from
--maps-file (out/soong/release-config/maps_list-<product>.txt) or --map, after the
build/release, device/*/release and vendor/*/release maps. Use it to see the flags a build
will ship without building it (BlissTweaks' refresh_bliss_tweaks_aconfig.sh --from-source).
"""
import argparse
import csv
import glob
import os
import re
import shutil
import subprocess
import sys

SKIP_DIRS = {"out", ".repo", ".git"}
FIELDS = ["package", "name", "namespace", "container", "fixed_read_only", "exported",
          "value", "value_from", "value_root", "permission", "description", "bug", "file"]


def walk_files(root, suffix, skip_top=True):
    for dp, dns, fns in os.walk(root):
        rel = os.path.relpath(dp, root)
        if skip_top and rel == ".":
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
        else:
            dns[:] = [d for d in dns if d != ".git"]
        for fn in fns:
            if fn.endswith(suffix):
                yield os.path.join(dp, fn)


def strip_comments(text):
    return re.sub(r"#[^\n]*", "", text)


def blocks(text, keyword):
    """Yield the bodies of `keyword { ... }` blocks (brace matched)."""
    for m in re.finditer(r"\b%s\s*\{" % keyword, text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        yield text[m.end():i - 1]


def field(body, key):
    m = re.search(r'\b%s:\s*"((?:[^"\\]|\\.)*)"' % key, body)
    return m.group(1) if m else ""


def flag(body, key):
    m = re.search(r"\b%s:\s*(true|false)" % key, body)
    return m.group(1) == "true" if m else False


def read_declarations(root):
    flags = {}
    for path in walk_files(root, ".aconfig"):
        try:
            text = strip_comments(open(path, errors="ignore").read())
        except OSError:
            continue
        package = field(text, "package")
        if not package:
            continue
        m = re.search(r'^\s*container:\s*"([^"]+)"', text, re.M)
        container = m.group(1) if m else ""
        for body in blocks(text, "flag"):
            name = field(body, "name")
            if not name:
                continue
            flags[(package, name)] = {
                "package": package, "name": name,
                "namespace": field(body, "namespace"), "container": container,
                "fixed_read_only": flag(body, "is_fixed_read_only"),
                "exported": flag(body, "is_exported"),
                "value": "DISABLED", "value_from": "default", "value_root": "",
                "permission": "",
                "description": " ".join(field(body, "description").split()),
                "bug": field(body, "bug"), "file": os.path.relpath(path, root),
            }
    return flags


def release_roots(root, exclude=()):
    maps = []
    for pattern in ("device/*/release", "device/*/*/release",
                    "vendor/*/release", "vendor/*/*/release"):
        maps += glob.glob(os.path.join(root, pattern, "release_config_map.textproto"))
    roots = [os.path.join(root, "build", "release")]
    roots += [os.path.dirname(m) for m in sorted(os.path.relpath(m, root) for m in maps)]
    skip = {os.path.normpath(os.path.join(root, e)) for e in exclude}
    return [os.path.join(root, r) if not os.path.isabs(r) else r for r in roots
            if os.path.normpath(os.path.join(root, r)) not in skip]


def read_inherits(roots):
    inherits = {}
    for r in roots:
        for path in walk_files(os.path.join(r, "release_configs"), ".textproto", False):
            text = strip_comments(open(path, errors="ignore").read())
            name = field(text, "name")
            if name:
                parents = re.findall(r'\binherits:\s*"([^"]+)"', text)
                inherits.setdefault(name, [])
                inherits[name] += [p for p in parents if p not in inherits[name]]
    return inherits


def release_chain(release, inherits, seen=None):
    """Parents first (in listed order), then the release itself."""
    seen = seen if seen is not None else set()
    if release in seen:
        return []
    seen.add(release)
    chain = []
    for parent in inherits.get(release, []):
        chain += [c for c in release_chain(parent, inherits, seen) if c not in chain]
    return chain + [release]


def apply_release_values(flags, roots, chain, top):
    for rel in chain:
        for r in roots:
            d = os.path.join(r, "aconfig", rel)
            if not os.path.isdir(d):
                continue
            for path in walk_files(d, ".textproto", False):
                text = strip_comments(open(path, errors="ignore").read())
                for body in blocks(text, "flag_value"):
                    key = (field(body, "package"), field(body, "name"))
                    m = re.search(r"\bstate:\s*(\w+)", body)
                    p = re.search(r"\bpermission:\s*(\w+)", body)
                    if key in flags and m:
                        flags[key]["value"] = m.group(1)
                        flags[key]["value_from"] = rel
                        flags[key]["value_root"] = os.path.relpath(r, top)
                        flags[key]["permission"] = p.group(1) if p else ""


def cmd_scan(args):
    root = os.path.abspath(args.root)
    flags = read_declarations(root)
    roots = release_roots(root, args.exclude_root or ())
    chain = release_chain(args.release, read_inherits(roots))
    apply_release_values(flags, roots, chain, root)
    out = open(args.output, "w", newline="") if args.output else sys.stdout
    w = csv.DictWriter(out, fieldnames=FIELDS)
    w.writeheader()
    for key in sorted(flags):
        w.writerow(flags[key])
    print(f"{len(flags)} flags; release chain: {' -> '.join(chain)}", file=sys.stderr)


def load(path):
    return {(r["package"], r["name"]): r for r in csv.DictReader(open(path))}


def cmd_diff(args):
    old, new = load(args.old), load(args.new)
    spaces = set(filter(None, (args.namespaces or "").split(",")))
    keep = (lambda r: r["namespace"] in spaces) if spaces else (lambda r: True)
    added = [new[k] for k in sorted(new.keys() - old.keys()) if keep(new[k])]
    removed = [old[k] for k in sorted(old.keys() - new.keys()) if keep(old[k])]
    changed = []
    for k in sorted(old.keys() & new.keys()):
        o, n = old[k], new[k]
        if not keep(n):
            continue
        diffs = [f"{f}: {o[f]} -> {n[f]}" for f in ("value", "fixed_read_only", "namespace")
                 if o[f] != n[f]]
        if diffs:
            changed.append((n, diffs))
    out = open(args.output, "w") if args.output else sys.stdout

    def row(r):
        ro = "RO" if r["fixed_read_only"] == "True" else "rw"
        return (f"| `{r['package']}.{r['name']}` | {r['namespace']} | {ro} | {r['value']} "
                f"({r['value_from']}) | {r['description'][:140]} |")

    print(f"# aconfig diff: {os.path.basename(args.old)} -> {os.path.basename(args.new)}\n", file=out)
    if spaces:
        print(f"Namespaces: {', '.join(sorted(spaces))}\n", file=out)
    print(f"{len(added)} added, {len(removed)} removed, {len(changed)} changed.\n", file=out)
    header = "| Flag | Namespace | RO | Value (from) | Description |\n|---|---|---|---|---|"
    for title, rows in (("Added", added), ("Removed", removed)):
        print(f"## {title}\n\n{header}", file=out)
        for r in rows:
            print(row(r), file=out)
        print(file=out)
    print("## Changed\n\n| Flag | Change |\n|---|---|", file=out)
    for n, diffs in changed:
        print(f"| `{n['package']}.{n['name']}` | {'; '.join(diffs)} |", file=out)


def read_namespaces(args):
    spaces = list(filter(None, (args.namespaces or "").split(",")))
    if args.namespaces_file:
        for line in open(args.namespaces_file):
            line = line.split("#", 1)[0].strip()
            if line and line not in spaces:
                spaces.append(line)
    if not spaces:
        sys.exit("rw-pack: give --namespaces or --namespaces-file")
    return spaces


def cmd_rw_pack(args):
    flags = load(args.flags)
    spaces = read_namespaces(args)
    picked = {}
    for (package, name), r in sorted(flags.items()):
        if (r["namespace"] in spaces and r["fixed_read_only"] != "True"
                and r["permission"] == "READ_ONLY" and r["value_root"] == "build/release"):
            picked.setdefault(package, []).append(r)

    out = os.path.abspath(args.out_dir)
    os.makedirs(out, exist_ok=True)
    for entry in os.listdir(out):
        path = os.path.join(out, entry)
        if os.path.isdir(path) and glob.glob(os.path.join(path, "*_flag_values.textproto")):
            shutil.rmtree(path)

    generated = (f"Generated by device/generic/x86_64_tablet/scripts/aconfig_inventory.py rw-pack "
                 f"({len(spaces)} namespaces: {', '.join(spaces)}). Do not edit; regenerate.")
    modules = []
    for package, rows in picked.items():
        os.makedirs(os.path.join(out, package))
        with open(os.path.join(out, package, f"{args.prefix}_flag_values.textproto"), "w") as f:
            f.write(f"# {generated}\n")
            for r in rows:
                f.write(f'flag_value {{\n  package: "{package}"\n  name: "{r["name"]}"\n'
                        f'  state: {r["value"]}\n  permission: READ_WRITE\n}}\n')
        modules.append((f"aconfig-values-{args.prefix}-{args.release}-{package}-all", package))

    with open(os.path.join(out, "Android.bp"), "w") as f:
        f.write(f"// {generated}\n\npackage {{\n    default_applicable_licenses: "
                f"[\"{args.license}\"],\n}}\n")
        for module, package in modules:
            f.write(f'\naconfig_values {{\n    name: "{module}",\n    package: "{package}",\n'
                    f'    srcs: ["{package}/*_flag_values.textproto"],\n}}\n')
        f.write(f'\naconfig_value_set {{\n    name: "aconfig_value_set-{args.prefix}-{args.release}",\n'
                f'    values: [\n')
        for module, _ in modules:
            f.write(f'        "{module}",\n')
        f.write("    ],\n}\n")
    total = sum(len(v) for v in picked.values())
    print(f"{total} flags in {len(picked)} packages -> {out}", file=sys.stderr)


def bp_list(body, key):
    m = re.search(r"\b%s:\s*\[(.*?)\]" % key, body, re.S)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


def index_value_modules(map_dirs):
    """name -> ("set", [module names]) or ("values", package, [src files]), from Android.bp."""
    modules = {}
    for d in map_dirs:
        for bp in sorted(walk_files(d, "Android.bp", False)):
            text = re.sub(r"//[^\n]*", "", open(bp, errors="ignore").read())
            here = os.path.dirname(bp)
            for body in blocks(text, "aconfig_values"):
                srcs = []
                for pattern in bp_list(body, "srcs"):
                    srcs += sorted(glob.glob(os.path.join(here, pattern)))
                modules[field(body, "name")] = ("values", field(body, "package"), srcs)
            for body in blocks(text, "aconfig_value_set"):
                names = list(bp_list(body, "values"))
                for pattern in bp_list(body, "srcs"):
                    for sub in sorted(glob.glob(os.path.join(here, pattern))):
                        sub_text = re.sub(r"//[^\n]*", "", open(sub, errors="ignore").read())
                        names += [field(b, "name") for b in blocks(sub_text, "aconfig_values")]
                modules[field(body, "name")] = ("set", names)
    return modules


def release_value_sets(map_dirs, chain):
    sets = []
    for rel in chain:
        for d in map_dirs:
            path = os.path.join(d, "release_configs", rel + ".textproto")
            if os.path.isfile(path):
                text = strip_comments(open(path, errors="ignore").read())
                sets += [s for s in re.findall(r'\baconfig_value_sets:\s*"([^"]+)"', text)
                         if s not in sets]
    return sets


def release_flag(map_dirs, chain, name, default):
    value = default
    for rel in chain:
        for d in map_dirs:
            path = os.path.join(d, "flag_values", rel, name + ".textproto")
            if os.path.isfile(path):
                m = re.search(r"(bool_value|string_value):\s*(\"([^\"]*)\"|\w+)",
                              strip_comments(open(path).read()))
                if m:
                    value = m.group(3) if m.group(3) is not None else m.group(2)
    return value


def find_aconfig(root):
    for rel in ("out/host/linux-x86/bin/aconfig", "prebuilts/build-tools/linux-x86/bin/aconfig"):
        path = os.path.join(root, rel)
        if os.access(path, os.X_OK):
            return path
    return shutil.which("aconfig")


def cmd_caches(args):
    root = os.path.abspath(args.root)
    spaces = read_namespaces(args)
    map_dirs = [os.path.normpath(d) for d in release_roots(root, args.exclude_root or ())]
    extra = list(args.map or [])
    if args.maps_file and os.path.isfile(args.maps_file):
        extra += open(args.maps_file).read().split()
    for m in extra:
        d = os.path.normpath(os.path.join(root, os.path.dirname(m)))
        if d not in map_dirs:
            map_dirs.append(d)
    chain = release_chain(args.release, read_inherits(map_dirs))
    if args.release not in read_inherits(map_dirs):
        sys.exit(f"caches: no release config named {args.release}")
    permission = release_flag(map_dirs, chain, "RELEASE_ACONFIG_FLAG_DEFAULT_PERMISSION",
                              "READ_WRITE").lower()
    read_only = release_flag(map_dirs, chain, "RELEASE_ACONFIG_REQUIRE_ALL_READ_ONLY", "false")
    modules = index_value_modules(map_dirs)

    values = {}
    for set_name in release_value_sets(map_dirs, chain):
        entry = modules.get(set_name)
        if not entry:
            print(f"caches: value set {set_name} not found; skipped", file=sys.stderr)
            continue
        for name in entry[1]:
            mod = modules.get(name)
            if mod and mod[0] == "values":
                values.setdefault(mod[1], []).extend(mod[2])

    flags = read_declarations(root)
    decls, containers = {}, {}
    for (package, _), r in flags.items():
        decls.setdefault(package, set()).add(os.path.join(root, r["file"]))
        containers[package] = r["container"] or "system"
    packages = sorted({p for (p, _), r in flags.items() if r["namespace"] in spaces})

    aconfig = args.aconfig or find_aconfig(root)
    if not aconfig:
        sys.exit("caches: host aconfig tool not found")
    out = os.path.abspath(args.out_dir)
    os.makedirs(out, exist_ok=True)
    for old in glob.glob(os.path.join(out, "*.pb")):
        os.remove(old)
    written = 0
    for package in packages:
        files = sorted(decls[package])
        attempts = [files, [f for f in files if "/test" not in f.lower()]]
        for decl_files in attempts:
            cmd = [aconfig, "create-cache", "--package", package,
                   "--container", containers[package],
                   "--cache", os.path.join(out, package + ".pb"),
                   "--default-permission", permission,
                   "--allow-read-write", "false" if read_only in ("true", "1") else "true"]
            for f in decl_files:
                cmd += ["--declarations", f]
            for v in values.get(package, []):
                cmd += ["--values", v]
            p = subprocess.run(cmd, capture_output=True, text=True)
            if p.returncode == 0:
                written += 1
                break
        else:
            print(f"caches: {package}: {p.stderr.strip()[:200]}", file=sys.stderr)
    print(f"{written}/{len(packages)} package caches -> {out} (release {args.release}, "
          f"default permission {permission}, {len(map_dirs)} release maps)", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("root")
    s.add_argument("release")
    s.add_argument("-o", "--output")
    s.add_argument("--exclude-root", action="append")
    d = sub.add_parser("diff")
    d.add_argument("old")
    d.add_argument("new")
    d.add_argument("--namespaces")
    d.add_argument("-o", "--output")
    p = sub.add_parser("rw-pack")
    p.add_argument("flags")
    p.add_argument("release")
    p.add_argument("out_dir")
    p.add_argument("--namespaces")
    p.add_argument("--namespaces-file")
    p.add_argument("--prefix", default="bass-rw")
    p.add_argument("--license", default="Android-Apache-2.0")
    c = sub.add_parser("caches")
    c.add_argument("root")
    c.add_argument("release")
    c.add_argument("out_dir")
    c.add_argument("--namespaces")
    c.add_argument("--namespaces-file")
    c.add_argument("--maps-file")
    c.add_argument("--map", action="append")
    c.add_argument("--exclude-root", action="append")
    c.add_argument("--aconfig")
    args = ap.parse_args()
    {"scan": cmd_scan, "diff": cmd_diff, "rw-pack": cmd_rw_pack,
     "caches": cmd_caches}[args.cmd](args)


if __name__ == "__main__":
    main()
