# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.18.1: a script's commands read as the shell reads them when a quote
runs over several lines; an interpreter's code given on its command line is
no input file; the install for users is in the repo."""
import os
import subprocess

import pytest

from nomad.submit import apps, spec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

JOB = """#!/bin/bash
#SBATCH -J nomadtest
#SBATCH -c 2
python3 -c '
import time
a = b"x" * (300 * 1024 * 1024)
t = time.time()
while time.time() - t < 60:
    pass
'
echo done
"""


def test_a_quote_over_several_lines_is_one_command():
    opts, cmds = spec.sbatch_lines(JOB)
    assert opts == ["-J nomadtest", "-c 2"]
    assert len(cmds) == 2 and cmds[0].startswith("python3 -c '") and cmds[1] == "echo done"
    p = apps.program_of(cmds)
    assert (p.family, p.executable, p.input) == ("Python", "python3", None)


@pytest.mark.parametrize("piece, before, after", [
    ("echo 'open", None, "'"),
    ("still open", "'", "'"),
    ("closed' now", "'", None),
    ('say "a \\" b', None, '"'),
    ("echo it's # a comment", None, "'"),
    ("echo ok # it's a comment", None, None),
    ("echo \\'not a quote", None, None),
    ("echo $'it\\'s here'", None, None),
    ('echo "$(printf \'%s\' "it\'s")"', None, None),
    ("x=$(date", None, "("),
    ("echo `date", None, "`"),
])
def test_open_quotes(piece, before, after):
    assert spec._open_quote(piece, before) == after


@pytest.mark.parametrize("lines, exe, inp", [
    (["g16 < w.com > w.log"], "g16", "w.com"),
    (["echo 'a < b'; g16 < w.com"], "g16", "w.com"),
    (["python3 run.py --n 3"], "python3", "run.py"),
    (["python -m mypkg.train"], "python", None),
    (["python -m torch.distributed.run --nproc_per_node 4 train.py"], "python", "train.py"),
    (['g16 < "$INPUT" > out.log'], "g16", "$INPUT"),
    (['g16 < "my w.com" > out.log'], "g16", "my w.com"),
    (["g16 w.com 2>err.log"], "g16", "w.com"),
    (["python3 - <<EOF", "print(1)", "EOF"], "python3", None),
    (["Rscript -e 'x <- 1'"], "Rscript", None),
    (["Rscript fit.R"], "Rscript", "fit.R"),
    (["srun python3 -u train.py"], "python3", "train.py"),
    (["python3 -c \"import sys; print(sys.argv)\" w.com"], "python3", None),
])
def test_input_files_and_inline_code(lines, exe, inp):
    p = apps.program_of(spec.sbatch_lines("\n".join(lines))[1])
    assert (p.executable, p.input) == (exe, inp)


def test_backslash_lines_still_join():
    cmds = spec.sbatch_lines("srun \\\n  --mpi=pmix \\\n  vasp_std\n")[1]
    assert cmds == ["srun --mpi=pmix vasp_std"]


def _statements(text):
    """The script's lines, a line ending in a backslash joined to the next."""
    out, buf = [], ""
    for line in text.splitlines():
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    return out


def test_the_install_for_users_is_in_the_repo():
    path = os.path.join(ROOT, "scripts", "install_nomad_for_users.sh")
    assert os.access(path, os.X_OK)
    assert subprocess.run(["bash", "-n", path]).returncode == 0
    text = open(path).read()
    assert '$R/venv/bin/python" -I -B' in text                    # deaf to PYTHONHOME; no .pyc written
    assert "check_current.sh" in text and "NOMAD_USERS_DIR" in text
    assert 'UV_CACHE_DIR="$work/uv-cache"' in text and "work=$(mktemp -d)" in text   # no shared cache
    assert "UV_SHA256_x86_64=" in text and "--compile-bytecode" in text and "--no-bin" in text
    assert "flock -n 9" in text and 'mv -f "$D/bin/nomad.new" "$D/bin/nomad"' in text
    # the switch comes after the release is built and checked, and before old layouts go
    assert text.index('size --help </dev/null') < text.index('mv -f "$D/bin/nomad.new"') \
        < text.index('for x in venv python tools; do')
    # run as `bash -s VERSION < script`: nothing it runs may read the script from stdin
    for cmd in ('"$UV" pip install', '"$UV" python install', '"$UV" venv', 'curl -fsSL', "run_new() {"):
        for line in (ln for ln in _statements(text) if cmd in ln and "run_new size" not in ln):
            if cmd == "run_new() {":
                continue
            assert "</dev/null" in line, line


@pytest.mark.parametrize("text, exe, inp", [
    ("cat > w.com <<EOF\n%mem=2GB\n# b3lyp\n\nBob's water\n\n0 1\nO\nEOF\ng16 < w.com\n", "g16", "w.com"),
    ("cat > w.com <<'EOF'\nDon't\nEOF\ng16 < w.com\n", "g16", "w.com"),
    ('cat > w.com << "EOF"\nit\'s\nEOF\ng16 < w.com\n', "g16", "w.com"),
    ("cat > w.com <<-EOF\n\tit's\n\tEOF\ng16 < w.com\n", "g16", "w.com"),
    ("cat <<< \"it's\"\ng16 < w.com\n", "g16", "w.com"),
    ("echo $'it\\'s here'\ng16 < w.com\n", "g16", "w.com"),
    ('echo "$(printf \'%s\' "it\'s")"\ng16 < w.com\n', "g16", "w.com"),
    ("srun bash -c '\ncd sub\ng16 < w.com\n'\n", "g16", "w.com"),
    ("x=$(cat <<EOF\nBob's\nEOF\n)\ng16 < w.com\n", "g16", "w.com"),
    ("cat > w.com <<\\EOF\nBob's water\nEOF\ng16 < w.com\n", "g16", "w.com"),
    ("cat > w.com <<!\nBob's water\n!\ng16 < w.com\n", "g16", "w.com"),
    ("cat > w.com <<END-IN\nBob's water\nEND-IN\ng16 < w.com\n", "g16", "w.com"),
    ("files=$(ls *.com |  # don't forget\nsort)\ng16 < w.com\n", "g16", "w.com"),
    ("n=$(( 1 << bits ))\ng16 < w.com\n", "g16", "w.com"),
    ("echo hi # see <<EOF\ng16 < w.com\n", "g16", "w.com"),
    ("g16 < w.com>w.log\n", "g16", "w.com"),
    ("srun bash -c 'lfs setstripe -c 4 .'\ng16 < w.com\n", "g16", "w.com"),
])
def test_here_documents_and_quotes_dont_swallow_the_script(text, exe, inp):
    p = apps.program_of(spec.sbatch_lines(text)[1])
    assert (p.executable, p.input) == (exe, inp)


def test_bash_c_keeps_the_folder_and_launcher_around_it():
    p = apps.program_of(spec.sbatch_lines("cd sub\nsrun bash -c 'g16 < w.com'\n")[1])
    assert (p.executable, p.cd, p.launcher) == ("g16", "sub", "srun")
    p = apps.program_of(spec.sbatch_lines("mpirun -np 4 bash -c 'vasp_std'\n")[1])
    assert (p.executable, p.launcher) == ("vasp_std", "mpirun")


# Review 3: a here-document inside "$( )", two on one line, arithmetic,
# comments in backquotes, a body after a continued line.
@pytest.mark.parametrize("text", [
    'v="$(cat <<EOF\nBob\'s water\nEOF\n)"\ng16 < w.com\n',
    'v="$(\ncat <<EOF\nBob\'s water\nEOF\n)"\ng16 < w.com\n',
    'v=`cat <<EOF\nBob\'s water\nEOF\n`\ng16 < w.com\n',
    "x=$(cat <<EOF\nit's\nEOF\n)\necho \"$x\"\ng16 < w.com\n",
    "cat <<A <<B\nit's A\nA\nit's B\nB\ng16 < w.com\n",
    "echo $[1<<2]\ng16 < w.com\n",
    "(( n = 1 << 3 ))\ng16 < w.com\n",
    'echo "$(( 1 << 2 ))"\ng16 < w.com\n',
    "echo X `echo in # it's` Y\ng16 < w.com\n",
    "cat <<EOF \\\n| tr a b\nit's\nEOF\ng16 < w.com\n",
])
def test_review3_here_documents_and_arithmetic(text):
    cmds = spec.sbatch_lines(text)[1]
    assert cmds[-1] == "g16 < w.com", cmds                  # nothing swallowed
    p = apps.program_of(cmds)
    assert (p.executable, p.input) == ("g16", "w.com"), cmds


def test_a_body_inside_a_command_isnt_waited_for_again():
    cmds = spec.sbatch_lines("x=$(cat <<EOF\nit's\nEOF\n)\nmodule load g16\ng16 < w.com\n")[1]
    assert cmds[0].startswith("x=$(cat <<EOF") and cmds[1:] == ["module load g16", "g16 < w.com"]
    assert spec._scan(cmds[0]) == (None, [])
    assert apps.commands_of(cmds)[-1] == "g16 < w.com"


@pytest.mark.parametrize("line, after", [
    ('v="$(cat <<EOF', '"('),
    ("echo `date # it's`", None),
    ("echo $(( 1 << 2 ))", None),
    ("echo $[ 1 << 2 ]", None),
])
def test_scan_contexts(line, after):
    assert spec._open_quote(line, None) == after


def test_scan_finds_here_documents_in_shell_context_only():
    assert spec._scan('v="$(cat <<EOF')[1] == [("EOF", False)]
    assert spec._scan("cat <<-A <<'B'")[1] == [("A", True), ("B", False)]
    assert spec._scan("echo '<<EOF'")[1] == []
    assert spec._scan("echo $(( 1 << n ))")[1] == []
    assert spec._scan("echo hi # <<EOF")[1] == []
    assert spec._scan("cat <<< 'x'")[1] == []


def test_bash_c_under_a_folder_nomad_doesnt_know():
    p = apps.program_of(spec.sbatch_lines("cd $SCRATCH/x\nsrun bash -c 'cd sub && g16 < w.com'\n")[1])
    assert (p.executable, p.cd) == ("g16", "?")
    p = apps.program_of(spec.sbatch_lines("cd /data/x\nsrun bash -c 'cd sub && g16 < w.com'\n")[1])
    assert p.cd == "/data/x/sub"


def test_the_install_checks_the_folder_before_touching_it():
    text = open(os.path.join(ROOT, "scripts", "install_nomad_for_users.sh")).read()
    assert text.index('not_ours "venv/ isn\'t 1.18.0\'s"') < text.index("exec 9>")
    assert "gone()" in text and "put_aside()" in text and "|| true)" in text   # removals after the switch don't stop it
    assert text.index("switched=yes") < text.index('mv -f "$D/bin/nomad.new" "$D/bin/nomad"')


# Review 4: arithmetic only when it closes with )), a subshell in $( ),
# here-document bodies after a string closes, case in $( ), parting commands.
@pytest.mark.parametrize("text", [
    "n=$((cd /tmp && ls) | wc -l)\ng16 < w.com\n",
    "((sleep 3500; scancel $SLURM_JOB_ID) &)\ng16 < w.com\n",
    "while (( i << 1 < 8 )); do i=$((i+1)); done\ng16 < w.com\n",
    "if (( SLURM_ARRAY_TASK_ID << 1 > 4 )); then echo big; fi\ng16 < w.com\n",
    "x=$(echo $(( $(nproc) << 1 )))\ng16 < w.com\n",
    "x=`cat <<EOF\nhi it's\nEOF`\ng16 < w.com\n",
    "cat <<EOF | awk '\n{ print \"x\", $1 }'\nit's body\nEOF\ng16 < w.com\n",
    'msg="$(case $HOSTNAME in\n  gpu*) echo "it\'s a gpu node" ;;\n  *) echo cpu ;;\nesac)"\ng16 < w.com\n',
    "x=$(echo case closed)\necho \"it's\"\ng16 < w.com\n",
    "x=$(grep -c esac log)\ng16 < w.com\n",
])
def test_review4_scripts(text):
    cmds = spec.sbatch_lines(text)[1]
    assert cmds[-1] == "g16 < w.com", cmds
    p = apps.program_of(cmds)
    assert (p.executable, p.input) == ("g16", "w.com"), cmds


def test_commands_part_outside_here_documents_and_comments():
    cmds = spec.sbatch_lines("x=$(cat <<EOF\nit's; a && b\nEOF\n) && g16 < w.com\n")[1]
    assert apps.commands_of(cmds)[-1] == "g16 < w.com"
    assert spec.split_commands("echo 'a; b' && cd x; g16 < w.com # c; d") == \
        ["echo 'a; b'", "cd x", "g16 < w.com # c; d"]
