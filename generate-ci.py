# ==================================================================================================
# Copyright (c) 2022, Jairus Martin.
# Distributed under the terms of the GPL v3 License.
# The full license is in the file LICENSE, distributed with this software.
# Created on Feb 4, 2022
# ==================================================================================================
import os
import copy
import yaml
import subprocess
from yaml.representer import SafeRepresenter

PY_VER = "3.10"
NDK_VER = "23.1.7779620"

NDK_TEMPLATE = f"""
export ANDROID_HOME="$HOME/Android/Sdk"
export ANDROID_SDK_ROOT="$HOME/Android/Sdk"
export PATH="$PATH:$ANDROID_HOME/cmdline-tools/latest/bin"
mkdir -p $ANDROID_HOME
wget -q https://dl.google.com/android/repository/commandlinetools-linux-8092744_latest.zip
unzip -e commandlinetools-linux-8092744_latest.zip -d $ANDROID_HOME/cmdline-tools
mv $ANDROID_HOME/cmdline-tools/cmdline-tools $ANDROID_HOME/cmdline-tools/latest
yes | sdkmanager --licenses > /dev/null
sdkmanager --install tools
sdkmanager --install platform-tools
sdkmanager --install "ndk;{NDK_VER}"
"""

# Patch conda build because it fails cleaning up optimized pyc files
conda_post = "$HOME/micromamba/envs/conda-mobile/lib/python3.10/site-packages/conda_build/post.py"

SETUP = """
sed -i 's/.match(fn):/.match(fn) and exists(join(prefix, fn)):/g' %s
""" % (
    conda_post,
)


class Block(str):
    @staticmethod
    def render(dumper, data):
        s = SafeRepresenter.represent_str(dumper, data.lstrip())
        s.style = "|"
        return s


def build_requirements(meta, all_packages) -> set[str]:
    """Read list of build requirements from meta file"""
    needs = set()
    if "requirements" in meta:
        reqs = meta["requirements"]
        for section in ("build", "run"):
            if section in reqs:
                for r in reqs[section]:
                    dep_name = r.split()[0]
                    if dep_name in all_packages:
                        needs.add(dep_name)
    return needs


def all_build_requirements(pkg, package_deps) -> set[str]:
    """Build a recursive list of all build requirements including dependencies
    of dependencies

    """
    needs = package_deps[pkg]
    deps_needed = set()
    for dep in needs:
        deps_needed.update(all_build_requirements(dep, package_deps))
    reqs = list(needs.union(deps_needed))
    reqs.sort()
    return reqs


def main():
    # Render blocks as | literal
    yaml.add_representer(Block, Block.render)

    # Map package group to names (eg all android-* under one key)
    packages = {}
    # Load meta files
    package_meta = {}

    allowed_groups = (
        "pip",
        # "ios",
        "android",
    )
    all_packages = set()
    for item in os.listdir("."):
        if os.path.isdir(item) and not item[0] == ".":
            group, *name = item.split("-", 1)
            if group not in allowed_groups:
                continue
            meta_file = f"{item}/meta.yaml"

            if not os.path.exists(meta_file):
                continue
            print(f"Loading {meta_file}")
            data = subprocess.check_output(f"boa convert {meta_file}".split())
            with open(f"{item}/recipe.yaml", "wb") as f:
                f.write(data)

            data = data.decode()
            meta = yaml.load(data, yaml.Loader)
            package_meta[item] = meta

            # FIXME: Old recipes...
            if "externally-managed" in data:
                continue  # TODO: Old pip recipe
            build_sh = f"{item}/build.sh"
            if os.path.exists(build_sh):
                with open(build_sh) as f:
                    build_script = f.read()
                if "ndk-bundle" in build_script:
                    continue  # TODO: Old recipe

            all_packages.add(item)
            if group in packages:
                packages[group].append(item)
            else:
                packages[group] = [item]

    # Immediate dependencies of all packages
    package_deps = {}
    for pkg in all_packages:
        meta = package_meta[pkg]
        package_deps[pkg] = build_requirements(meta, all_packages)

    jobs = {}
    conda_bld_path = "~/micromamba/envs/conda-mobile/conda-bld"

    common_steps = [
        {"uses": "actions/checkout@v2"},
        {
            "name": "Install micromamba",
            "uses": "mamba-org/provision-with-micromamba@main",
            "with": {
                # "cache-env": True,
                "cache-downloads": True,
            },
        },
        {"name": "Setup micromamba", "shell": "bash -l {0}", "run": Block(SETUP)},
    ]

    android_steps = [
        {
            "name": "Install system deps",
            "run": "sudo apt-get install -y autopoint texinfo rename patchelf",
        },
        {
            "name": "Setup JDK",
            "uses": "actions/setup-java@v1",
            "with": {"java-version": 11},
        },
        {
            "name": "Cache Android NDK",
            "uses": "actions/cache@v2",
            "id": "android-cache",
            "with": {"path": "~/Android", "key": f"linux-android-ndk-{NDK_VER}"},
        },
        {
            "name": "Setup android NDK",
            "if": "steps.android-cache.outputs.cache-hit != 'true'",
            "run": Block(NDK_TEMPLATE),
        },
    ]

    for group, items in packages.items():
        runs_on = "ubuntu-latest"
        group_steps = []
        if group == "android":
            group_steps = android_steps
        elif group == "ios":
            runs_on = "macos-latest"

        for pkg in items:
            meta = package_meta[pkg]
            build_steps = [
                {
                    "name": "Convert recipe",
                    "shell": "bash -l {0}",
                    "run": f"boa convert {pkg}/meta.yaml > {pkg}/recipe.yaml",
                },
                {
                    "name": "Build recipe",
                    "shell": "bash -l {0}",
                    "run": f"boa build {pkg}",
                },
                {
                    "name": "Upload package",
                    "uses": "actions/upload-artifact@v2",
                    "with": {
                        "name": f"{pkg}-{PY_VER}",
                        "path": f"{conda_bld_path}/*/{pkg}*.bz2",
                    },
                },
            ]

            # Generate steps to download and install requirements
            req_steps = []
            needs = all_build_requirements(pkg, package_deps)
            if needs:
                for req in needs:
                    req_steps.append(
                        {
                            "name": f"Download {req}",
                            "uses": "actions/download-artifact@v2",
                            "with": {
                                "name": f"{req}-{PY_VER}",
                                "path": f"{conda_bld_path}/",
                            },
                        }
                    )
                req_steps.append(
                    {
                        "name": f"Run conda index",
                        "shell": "bash -l {0}",
                        "run": f"conda index {conda_bld_path}/*",
                    }
                )

            steps = common_steps + group_steps + req_steps + build_steps

            job = jobs[pkg] = {
                "runs-on": runs_on,
                "steps": copy.deepcopy(steps),
            }
            if needs:
                job["needs"] = needs

    # Try one for now..
    # jobs = {'android-ndk': jobs['android-ndk']}
    script = {
        "name": "CI",
        "on": "push",
        "jobs": jobs,
    }

    with open(".github/workflows/ci.yml", "w") as f:
        f.write(yaml.dump(script))


def convert_boa():
    import subprocess
    from glob import glob

    for meta in glob("*/meta.yaml"):
        path, name = os.path.split(meta)
        result = subprocess.check_output(["boa", "convert", meta])
        with open(os.path.join(path, "recipe.yaml"), "wb") as f:
            f.write(result)


if __name__ == "__main__":
    main()
