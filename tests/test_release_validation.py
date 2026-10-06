"""Release metadata must agree before a tag can be published."""

import pytest

from scripts.check_release import validate_release


@pytest.fixture
def release_metadata():
    return ({'project': {'name': 'ai-agent-startup', 'version': '1.2.3'}},
            {'package': [{'name': 'ai-agent-startup', 'version': '1.2.3'}]},
            '## [1.2.3] - 2026-10-06\n')


def test_matching_release_metadata_passes(release_metadata):
    validate_release('v1.2.3', *release_metadata)


@pytest.mark.parametrize('tag', ['1.2.3', 'v1.2', 'v01.2.3', 'v1.2.3rc1', 'v1.2.3\n'])
def test_invalid_release_tag_is_rejected(release_metadata, tag):
    with pytest.raises(ValueError, match='Release tag'):
        validate_release(tag, *release_metadata)


def test_mismatched_package_version_is_rejected(release_metadata):
    project, lock, changelog = release_metadata
    project['project']['version'] = '1.2.4'
    with pytest.raises(ValueError, match='project version'):
        validate_release('v1.2.3', project, lock, changelog)


@pytest.mark.parametrize('entries', [[],
    [{'name': 'ai-agent-startup', 'version': '1.2.4'}],
    [{'name': 'ai-agent-startup', 'version': '1.2.3'}] * 2])
def test_missing_mismatched_or_duplicate_lock_package_is_rejected(release_metadata, entries):
    project, lock, changelog = release_metadata
    lock['package'] = entries
    with pytest.raises(ValueError, match='exactly one'):
        validate_release('v1.2.3', project, lock, changelog)


@pytest.mark.parametrize('changelog', ['', '## [1.2.4] - 2026-10-06\n',
    '## [1.2.3]\n', '## [1.2.3] - undated\n'])
def test_missing_dated_changelog_entry_is_rejected(release_metadata, changelog):
    project, lock, _ = release_metadata
    with pytest.raises(ValueError, match='CHANGELOG'):
        validate_release('v1.2.3', project, lock, changelog)
