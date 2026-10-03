"""
季号的兜底：season_number 为 None 时，任务不能无声消失。

`determine_season_number` 以前没有 return 兜底 —— B 站把一部剧的每一季拆成彼此独立的
条目，seasons 列表还可能横跨不相干的几部剧（央视版《四大名著》共用一个列表），
当前 season_id 未必在这份列表里，于是它返回 None。None 一路传到命名规则的
`{season_number:02d}`，format(None, "02d") 抛 TypeError，`__update_file_name_info`
再抛 ValueError，整条任务**建不出来**，界面上只看到「创建下载任务失败 N 条」
（2026-10-02 一次批量丢了 30 条，日志里 30 行同样的堆栈）。

两层都要有：数据源头给一个合法季号，变量表对 None 再兜一层 —— 后者是防"别的
来源又塞进一个 None"，而渲染器对 None 毫无招架之力。
"""

from util.download.task.info import TaskInfo
from util.format.file_name import FileNameFormatter
from util.parse.episode.bangumi import BangumiEpisodeParser

import pytest


def make_parser(season_id, seasons):
    parser = BangumiEpisodeParser.__new__(BangumiEpisodeParser)

    parser.info_data = {"season_id": season_id, "seasons": seasons}

    return parser


def test_season_number_is_the_position_in_the_season_list():
    parser = make_parser(22, [{"season_id": 11}, {"season_id": 22}, {"season_id": 33}])

    assert parser.determine_season_number() == 2


def test_unknown_season_falls_back_to_one():
    """
    当前 season_id 不在列表里时给 1，绝不返回 None

    返回 None 的代价是整条任务消失（见模块开头）。季号本来就能被「名称识别」规则
    改写，退化成第 1 季远比丢任务轻
    """
    parser = make_parser(999, [{"season_id": 11}, {"season_id": 22}])

    assert parser.determine_season_number() == 1


def test_empty_season_list_does_not_crash():
    parser = make_parser(33622, [])

    assert parser.determine_season_number() == 1


def test_none_season_number_becomes_one_in_the_variable_table():
    task_info = TaskInfo()

    task_info.Episode.season_title = "测试剧"
    task_info.Episode.season_number = None
    task_info.Episode.episode_number = 3

    formatter = FileNameFormatter()
    formatter.set_variable_data(task_info)

    assert formatter.variable_data["season_number"] == 1
    assert formatter.variable_data["episode_number"] == 3


def test_none_episode_number_and_part_number_become_zero():
    task_info = TaskInfo()

    task_info.Episode.season_number = None
    task_info.Episode.episode_number = None
    task_info.Episode.part_number = None

    formatter = FileNameFormatter()
    formatter.set_variable_data(task_info)

    assert formatter.variable_data["episode_number"] == 0
    assert formatter.variable_data["p"] == 0


def test_a_none_season_number_still_renders_a_file_name():
    """
    端到端：带 :02d 的规则在 season_number 为 None 时仍然能渲染出文件名

    attribute 留 0：非零时 format() 会去取"特殊规则"覆盖这里塞进去的 rule
    """
    task_info = TaskInfo()

    task_info.Episode.season_title = "测试剧"
    task_info.Episode.season_number = None
    task_info.Episode.episode_number = 3

    formatter = FileNameFormatter()
    formatter.set_variable_data(task_info)
    formatter.set_rule("{season_title}.S{season_number:02d}E{episode_number:02d}")

    assert formatter.format() == "测试剧.S01E03"


def test_none_really_does_break_a_format_specifier():
    """留一份证据：不做兜底时它必然抛 TypeError，所以上面那两条不是多余的"""
    with pytest.raises(TypeError):
        "{:02d}".format(None)
