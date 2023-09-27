import json
import os
import time
import traceback
from typing import Iterable

import fire
import xmltodict

from config import (CHANGESETS_LIMIT_CONFIG, CHANGESETS_LIMIT_MODERATOR_REVERT,
                    CREATED_BY, WEBSITE)
from diff_entry import DiffEntry
from invert import Inverter
from osm import OsmApi, build_osm_change
from overpass import Overpass
from utils import ensure_iterable, is_osm_moderator


def build_element_ids_dict(element_ids: Iterable[str]) -> dict[str, dict[str, set[str]]]:
    result = {
        'include': {
            'node': set(),
            'way': set(),
            'relation': set()
        },
        'exclude': {
            'node': set(),
            'way': set(),
            'relation': set()
        }
    }

    prefixes = {
        'node': ('nodes', 'node', 'n'),
        'way': ('ways', 'way', 'w'),
        'relation': ('relations', 'relation', 'rel', 'r')
    }

    for element_id in element_ids:
        element_id = element_id.strip().lower()

        if element_id.startswith('-'):
            element_result = result['exclude']
            element_id = element_id.lstrip('-').lstrip()
        else:
            element_result = result['include']
            element_id = element_id.lstrip('+').lstrip()

        for element_type, value in prefixes.items():
            for prefix in value:
                if element_id.startswith(prefix):
                    element_id = element_id[len(prefix):].lstrip(':;.,').lstrip()

                    if not element_id.isnumeric():
                        raise Exception(f'{element_type.title()} element id must be numeric: {element_id}')

                    element_result[element_type].add(element_id)
                    break
            else:
                continue
            break

        else:
            raise Exception(f'Unknown element filter format: {element_id}')

    return result


def merge_and_sort_diffs(diffs: list[dict[str, list[DiffEntry]]]) -> dict[str, list[DiffEntry]]:
    result = diffs[0]

    for diff in diffs[1:]:
        for element_type, elements in diff.items():
            result[element_type] += elements

    for element_type, elements in result.items():
        # sort by newest edits first
        result[element_type] = sorted(elements, key=lambda t: t.timestamp, reverse=True)

    return result


def filter_discussion_changesets(changeset_ids: list[str], target: str) -> list[str]:
    if target == 'all':
        return changeset_ids
    if target == 'newest':
        return changeset_ids[-1:]
    if target == 'oldest':
        return changeset_ids[:1]

    print(f'🚧 Warning: Unknown discussion target: {target}')
    return []


def print_warn_elements(warn_elements: dict[str, list[str]]) -> None:
    for element_type, element_ids in warn_elements.items():
        for element_id in element_ids:
            print(f'⚠️ Please verify: https://www.openstreetmap.org/{element_type}/{element_id}')


def main_timer(func):
    def wrapper(*args, **kwargs):
        start_time = time.perf_counter()

        try:
            exit_code = func(*args, **kwargs)
        except Exception:
            traceback.print_exc()
            exit_code = -2

        total_time = time.perf_counter() - start_time
        print(f'🏁 Total time: {total_time:.1F} sec')
        exit(exit_code)

    return wrapper


# TODO: improved revert to date
# TODO: filter does not include nodes if way was unmodified
# https://overpass-api.de/achavi/?changeset=131696060
# https://www.openstreetmap.org/way/357241890/history

# TODO: Exit code: None ? (after success and total time)
# TODO: util function to ensure tags existence and type
# TODO: slow but very accurate revert (download full history of future edits); overpass-api: timeline
# TODO: dataclasses
@main_timer
def main(changeset_ids: list | str | int, comment: str,
         username: str = None, password: str = None, *,
         oauth_token: str = None,
         discussion: str = None, discussion_target: str = None,
         osc_file: str = None, print_osc: bool = None,
         query_filter: str = '', only_tags: list | str | int = '') -> int:
    changeset_ids = tuple(sorted(set(
        str(cs_id).strip()
        for cs_id in ensure_iterable(changeset_ids)
        if cs_id
    )))
    assert changeset_ids, 'Missing changeset id'
    assert all(c.isnumeric() for c in changeset_ids), 'Changeset ids must be numeric'

    only_tags = frozenset(
        str(only_tag).strip()
        for only_tag in ensure_iterable(only_tags)
        if only_tag
    )

    if not username and not password:
        username = os.getenv('OSM_USERNAME')
        password = os.getenv('OSM_PASSWORD')

    if oauth_token:
        oauth_token: dict = json.loads(oauth_token)

    print('🔒️ Logging in to OpenStreetMap')
    osm = OsmApi(username=username, password=password, oauth_token=oauth_token)
    user = osm.get_authorized_user()

    user_edits = user['changesets']['count']
    user_is_moderator = is_osm_moderator(user['roles'])

    print(f'👤 Welcome, {user["display_name"]}{" 🔷" if user_is_moderator else ""}!')

    changesets_limit_config = CHANGESETS_LIMIT_CONFIG['moderator' if user_is_moderator else '']
    changesets_limit = max(v for k, v in changesets_limit_config.items() if k <= user_edits)

    if changesets_limit == 0:
        min_edits = min(k for k in changesets_limit_config.keys() if k > 0)
        print(f'🐥 You need to make at least {min_edits} edits to use this tool')
        return -1

    if changesets_limit < len(changeset_ids):
        print(f'🛟 For safety, you can only revert up to {changesets_limit} changesets at a time')

        if limit_increase := min((k for k in changesets_limit_config.keys() if k > user_edits), default=None):
            print(f'🛟 To increase this limit, make at least {limit_increase} edits')

        return -1

    overpass = Overpass()
    diffs = []

    for changeset_id in changeset_ids:
        changeset_id = int(changeset_id)
        print(f'☁️ Downloading changeset {changeset_id}')

        print(f'[1/?] OpenStreetMap …')
        changeset = osm.get_changeset(changeset_id)

        if user_edits < CHANGESETS_LIMIT_MODERATOR_REVERT and not user_is_moderator:
            changeset_user = osm.get_user(changeset['osm']['changeset']['@uid'])
            if changeset_user and is_osm_moderator(changeset_user['roles']):
                print(f'🛑 Moderators changesets cannot be reverted')
                return -1

        changeset_size = sum(len(v) for p in changeset['partition'].values() for v in p.values())
        partition_count = len(changeset['partition'])
        steps = partition_count + 1

        print(f'[1/{steps}] OpenStreetMap: {changeset_size} element{"s" if changeset_size > 1 else ""}')

        if changeset_size:
            if partition_count > 2:
                print(f'[2/{steps}] Overpass ({partition_count} partitions, this may take a while) …')
            else:
                print(f'[2/{steps}] Overpass ({partition_count} partition{"s" if partition_count > 1 else ""}) …')

            diff = overpass.get_changeset_elements_history(changeset, steps, query_filter)

            if not diff:
                return -1

            diffs.append(diff)
            diff_size = sum(len(el) for el in diff.values())

            assert diff_size <= changeset_size, \
                f'Diff must not be larger than changeset size: {diff_size=}, {changeset_size=}'

            if query_filter:
                print(f'[{steps}/{steps}] Overpass: {diff_size} element{"s" if diff_size > 1 else ""} (🪣 filtered)')
            else:
                print(f'[{steps}/{steps}] Overpass: {diff_size} element{"s" if diff_size > 1 else ""}')

    print('🔁 Generating a revert')
    merged_diffs = merge_and_sort_diffs(diffs)

    inverter = Inverter(only_tags)
    invert = inverter.invert_diff(merged_diffs)
    parents = overpass.update_parents(invert)

    if parents:
        print(f'🛠️ Fixing {parents} parent{"s" if parents > 1 else ""}')

    invert_size = sum(len(elements) for elements in invert.values())

    if invert_size == 0:
        print('✅ Nothing to revert')
        return 0

    if osc_file or print_osc:
        print(f'💾 Saving {invert_size} change{"s" if invert_size > 1 else ""} to .osc')

        osm_change = build_osm_change(invert, changeset_id=None)
        osm_change_xml = xmltodict.unparse(osm_change, pretty=True)

        if osc_file:
            with open(osc_file, 'w', encoding='utf-8') as f:
                f.write(osm_change_xml)

        if print_osc:
            print('<osc>')
            print(osm_change_xml)
            print('</osc>')

        print_warn_elements(inverter.warnings)
        print(f'✅ Success')
        return 0

    else:
        changeset_max_size = osm.get_changeset_max_size()

        if invert_size > changeset_max_size:
            print(f'🐘 Revert is too big: {invert_size} > {changeset_max_size}')

            if len(changeset_ids) > 1:
                print(f'🐘 Hint: Try reducing the amount of changesets to revert at once')

            return -1

        print(f'🌍️ Uploading {invert_size} change{"s" if invert_size > 1 else ""}')

        extra_args = {
            'changesets_count': user_edits + 1,
            'created_by': CREATED_BY,
            'website': WEBSITE
        }

        if len(changeset_ids) == 1:
            extra_args['id'] = ';'.join(f'https://www.openstreetmap.org/changeset/{c}' for c in changeset_ids)
        else:
            extra_args['id'] = ';'.join(changeset_ids)

        if query_filter:
            extra_args['filter'] = query_filter

        if changeset_id := osm.upload_diff(invert, comment, extra_args | inverter.statistics):
            changeset_url = f'https://www.openstreetmap.org/changeset/{changeset_id}'

            discussion = discussion.strip()

            if len(discussion) >= 4:  # prevent accidental discussions
                discussion += f'\n\n{changeset_url}'

                d_changeset_ids = filter_discussion_changesets(changeset_ids, discussion_target)
                print(f'💬 Discussing {len(d_changeset_ids)} changeset{"s" if len(d_changeset_ids) > 1 else ""}')

                for i, changeset_id in enumerate(d_changeset_ids, 1):
                    changeset_id = int(changeset_id)
                    status = osm.post_discussion_comment(changeset_id, discussion)
                    print(f'[{i}/{len(d_changeset_ids)}] Changeset {changeset_id}: {status}')

            print_warn_elements(inverter.warnings)
            print(f'✅ Success')
            print(f'✅ {changeset_url}')
            return 0

    return -1


if __name__ == '__main__':
    fire.Fire(main)
