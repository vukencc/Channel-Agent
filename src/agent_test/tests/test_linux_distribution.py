from pathlib import Path


def test_frozen_linux_data_root(monkeypatch, tmp_path):
    from agent_frontend.desktop.launcher import frozen_data_root
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'xdg'))
    assert frozen_data_root('linux') == tmp_path / 'xdg' / 'ChannelAgent'
    monkeypatch.setenv('XDG_DATA_HOME', 'relative')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    assert frozen_data_root('linux') == tmp_path / '.local/share/ChannelAgent'
