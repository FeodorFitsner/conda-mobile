# ==================================================================================================
# Copyright (c) 2022, Jairus Martin.
# Distributed under the terms of the GPL v3 License.
# The full license is in the file LICENSE, distributed with this software.
# Created on Feb 4, 2022
# ==================================================================================================
import os
import copy
import yaml
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
site_packages = '/usr/share/miniconda/lib/python3.9/site-packages'
CONDA_BUILD = f"""
conda install conda-build
sed -i 's/.match(fn):/.match(fn) and exists(join(prefix, fn)):/g' {site_packages}/conda_build/post.py
"""


class Block(str):
    @staticmethod
    def render(dumper, data):
        s = SafeRepresenter.represent_str(dumper, data.lstrip())
        s.style = "|"
        return s


def main():
    # Render blocks as | literal
    yaml.add_representer(Block, Block.render)

    packages = {}
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
            with open(meta_file) as f:
                data = f.read()

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

    jobs = {}
    conda_bld_path = "/usr/share/miniconda/conda-bld"

    common_steps = [
        {"uses": "actions/checkout@v2"},
        {
            "name": "Setup conda",
            "uses": "conda-incubator/setup-miniconda@v2",
            "with": {
                "auto-update-conda": True,
                "python-version": PY_VER,
            },
        },
        {"name": "Install conda build", "run": Block(CONDA_BUILD)},
    ]

    android_steps = [
        {
            "name": "Install system deps",
            "run": "sudo apt-get install -y autopoint texinfo rename",
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
            meta_file = f"{pkg}/meta.yaml"
            build_sh = f"{pkg}/build.sh"
            if not os.path.exists(meta_file):
                continue
            with open(meta_file) as f:
                data = f.read()

            # Add requirements
            needs = []
            if "requirements" in data:
                i = data.index("requirements:")
                meta = yaml.load(data[i:], yaml.Loader)
                reqs = meta["requirements"]
                if "build" in reqs:
                    for r in reqs["build"]:
                        dep_name = r.split()[0]
                        if dep_name in all_packages:
                            needs.append(dep_name)

            build_steps = [
                {
                    "name": "Build recipe",
                    "run": f"conda build --py={PY_VER} {pkg}",
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
            if needs:
                for req in needs:
                    req_steps.append(
                        {
                            "name": "Download requirements",
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

    script = {
        "name": "CI",
        "on": "push",
        "jobs": jobs,
    }

    with open(".github/workflows/ci.yml", "w") as f:
        f.write(yaml.dump(script))


if __name__ == "__main__":
    main()
