import subprocess
from pathlib import Path
from uuid import uuid4

from git import GitCommandError, Repo  # type: ignore
from openai.types.chat import ChatCompletionMessageParam

from mentat.code_feature import get_consolidated_feature_refs
from mentat.errors import SampleError
from mentat.git_handler import get_diff_commit, get_git_root_for_path, get_hexsha_active
from mentat.parsers.git_parser import GitParser
from mentat.sampler.sample import Sample
from mentat.session_context import SESSION_CONTEXT
from mentat.session_input import collect_user_input
from mentat.utils import get_relative_path


def parse_message(message: ChatCompletionMessageParam) -> dict[str, str]:
    ctx = SESSION_CONTEXT.get()
    content = message.get("content")
    text, code = "", ""
    if isinstance(content, str):
        if message.get("role") != "assistant":
            text = content
        output = list[str]()
        in_special = False
        for line in content.splitlines():
            if ctx.config.parser._starts_special(line):  # type: ignore
                in_special = True
            if not in_special:
                output.append(line)
            else:
                pass  # TODO: Convert to git diff format, replace 'code' above
            if ctx.config.parser._ends_code(line):  # type: ignore
                in_special = False
        while output[-1] == "":
            output.pop()
        text = "\n".join(output)
    elif isinstance(content, list) and len(content) > 0:
        content = content[0]
        if "text" in content and isinstance(content.get("text"), str):  # type: ignore
            text = content.get("text")  # type: ignore
    return {"text": text, "code": code}


def get_active_snapshot_commit(repo: Repo) -> str | None:
    """Returns the commit hash of the current active snapshot, or None if there are no active changes."""
    if not repo.is_dirty():
        return None
    try:
        # Stash active changes and record the current position
        repo.git.add("--all")  # So new files are included
        repo.git.stash("push", "-u")
        detached_head = repo.head.is_detached
        if detached_head:
            current_state = repo.head.commit.hexsha
        else:
            current_state = repo.active_branch.name
        # Commit them on a temporary branch
        temp_branch = f"sample_{uuid4().hex}"
        repo.git.checkout("-b", temp_branch)
        repo.git.stash("apply")
        repo.git.commit("-am", temp_branch)
        # Save the commit hash for diffing against later
        new_commit = repo.head.commit.hexsha
        # Reset repo to how it was before
        repo.git.checkout(current_state)
        repo.git.branch("-D", temp_branch)
        repo.git.stash("apply")
        repo.git.stash("drop")
        # Return the hexsha of the new commit
        return new_commit

    except Exception as e:
        raise SampleError(
            "WARNING: Mentat encountered an error while making temporary git changes:"
            f" {e}. If your active changes have disappeared, they can most likely be "
            "recovered using 'git stash pop'."
        )


class Sampler:
    diff_active: str | None = None
    commit_active: str | None = None
    last_sample_id: str | None = None
    last_sample_hexsha: str | None = None

    def set_active_diff(self):
        # Create a temporary commit with the active changes
        ctx = SESSION_CONTEXT.get()
        if not get_git_root_for_path(ctx.cwd, raise_error=False):
            return
        repo = Repo(ctx.cwd)
        self.commit_active = get_active_snapshot_commit(repo)
        # If changes were made since the last sample, don't list it as parent.
        if not self.last_sample_hexsha:
            return
        if self.last_sample_hexsha != get_hexsha_active():
            self.last_sample_id = None
            self.last_sample_hexsha = None

    async def create_sample(self) -> Sample:
        # Check for repo and merge_base in config
        session_context = SESSION_CONTEXT.get()
        stream = session_context.stream
        code_context = session_context.code_context
        config = session_context.config
        conversation = session_context.conversation
        cwd = session_context.cwd

        stream.send("Input sample data", color="light_blue")
        git_repo = Repo(cwd)
        merge_base = None
        if config.sample_merge_base_target:
            target = config.sample_merge_base_target
            stream.send(f"Use merge base target from config ({target})? (y/N)")
            response = (await collect_user_input()).data.strip()
            if response == "y":
                try:
                    mb = git_repo.merge_base(git_repo.head.commit, target)[0]
                    assert mb and hasattr(mb, "hexsha"), "No merge base found"
                    merge_base = mb.hexsha
                except Exception as e:
                    stream.send(
                        f"Error getting merge base from tar: {e}", color="light_red"
                    )
        if not merge_base:
            merge_base = git_repo.head.commit.hexsha
            stream.send(
                f"Use latest commit ({merge_base[:10]} as merge base? Press 'ENTER' to"
                " accept, or enter a new merge base commit."
            )
            response = (await collect_user_input()).data.strip()
            if response:
                merge_base = response
        try:
            assert merge_base is not None, "No merge base found"
            diff_merge_base = get_diff_commit(merge_base, "HEAD")
        except (AssertionError, GitCommandError) as e:
            raise SampleError(f"Error getting diff for merge base: {e}")

        repo = config.sample_repo
        if not repo:
            remote_url = ""
            try:
                remote_url = subprocess.check_output(
                    ["git", "config", "--get", "remote.origin.url"],
                    text=True,
                ).strip()
            except subprocess.CalledProcessError:
                pass
            stream.send(
                f"Found repo URL: {remote_url}. Press 'ENTER' to accept, or enter a new"
                " URL."
            )
            response = (await collect_user_input()).data.strip()
            if response == "y":
                repo = remote_url
            else:
                repo = response
            config.sample_repo = repo

        stream.send("Sample Title:")
        title = (await collect_user_input()).data.strip() or ""
        stream.send("Description: (optional)")
        description = (await collect_user_input()).data.strip() or ""
        stream.send("Test Command: (optional, e.g. 'pytest -k foo')")
        test_command = (await collect_user_input()).data.strip() or ""

        message_history, message_prompt, message_edit = list[dict[str, str]](), "", ""
        for m in conversation.get_messages()[::-1]:
            if m["role"] not in {"user", "assistant"}:
                continue
            parsed = parse_message(m)
            text = parsed["text"]
            code = parsed["code"]
            if not message_prompt:
                if m["role"] == "user":
                    message_prompt = text
                elif m["role"] == "assistant":
                    message_edit += text
            else:
                message_history.append({"role": m["role"], "content": text + code})

        diff_active = ""
        diff_edit = get_diff_commit("HEAD")
        if self.commit_active:
            diff_active = get_diff_commit("HEAD", self.commit_active)
            diff_edit = get_diff_commit(self.commit_active)

        context = set[str]()

        def _rp(f: str | Path) -> str:
            return get_relative_path(Path(f), cwd).as_posix()

        # Add include_files from context
        if code_context.include_files:
            feature_refs = get_consolidated_feature_refs(
                [f for fs in code_context.include_files.values() for f in fs]
            )
            context.update(_rp(f) for f in feature_refs)
        # Undo adds/removes/renames to match pre-diff_edit state
        file_edits = GitParser().parse_string(diff_edit).file_edits
        for file_edit in file_edits:
            file_path = _rp(file_edit.file_path)
            rename_path = (
                ""
                if not file_edit.rename_file_path
                else _rp(file_edit.rename_file_path)
            )
            if file_edit.is_deletion or rename_path or file_edit.replacements:
                context.add(file_path)
            if file_edit.is_creation and file_path in context:
                context.remove(file_path)
            if rename_path and rename_path in context:
                context.remove(rename_path)

        sample = Sample(
            title=title,
            description=description,
            id=uuid4().hex,
            parent_id=self.last_sample_id or "",
            repo=repo,
            merge_base=merge_base,
            diff_merge_base=diff_merge_base,
            diff_active=diff_active,
            message_history=message_history,
            message_prompt=message_prompt,
            message_edit=message_edit,
            context=list(context),
            diff_edit=diff_edit,
            test_command=test_command,
        )

        # Save the hexsha and id
        self.last_sample_id = sample.id
        self.last_sample_hexsha = get_hexsha_active()
        self.commit_active = None

        return sample
