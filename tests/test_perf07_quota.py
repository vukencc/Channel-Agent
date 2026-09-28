from ai_agent_startup.tools import command


def test_chatty_command_throttles_scans_and_keeps_boundaries(sandbox_env, monkeypatch):
    calls = []
    original = command.check_workspace_quota
    def checked():
        calls.append(1)
        return original()
    monkeypatch.setattr(command, 'check_workspace_quota', checked)
    result = command.run_command('python3 -c "import os,time; '
        "[(os.write(1,b'x'*8192),time.sleep(.005)) for _ in range(50)]\"")
    assert result.startswith('[退出码] 0'), result
    assert 3 <= len(calls) < 15
