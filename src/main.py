"""SoccerSim 策略入口 —— 比赛策略主逻辑都在这里,改打法就改这个文件。

结构(由浅入深):
- main.py(本文件):比赛策略。play() 按 Phase 状态机分派到 _act_*;各 _act_* 选出
  attacker(离球最近)并直接调 player 动作。
- player.py:Player 控制 handle + 高层动作(attack / take_kickoff /
  move_to_position / walk_to);想加拐棍/技术动作直接改它。
- utils/:走位/几何/避障工具(opponent_goal / dist / angle_to ...)。
- framework/:平台管线,用户不改。

改打法主要改本文件:Phase 状态机、各 _act_* 行为、站位公式。
"""

from __future__ import annotations

import logging
import math
from enum import Enum

from booster_agent_framework import AgentBase

from .framework.agent import SoccerAgentMixin
from .framework.types import KICKING_TEAM_NONE, Context, GameState, SetPlay
from .param import *
from .player import Player
from .utils import clamp_inside_field, dist, opponent_goal, own_goal


_log = logging.getLogger(__name__)


# ======================================================================
# Phase 状态机 —— 比赛阶段分类
# ======================================================================


class Phase(Enum):
    """比赛阶段。顶层状态机,决定当前是正常拼抢/开球/定位球/准备/停止。"""
    NORMAL = "normal"              # PLAYING 正常拼抢
    OUR_KICKOFF = "our_kickoff"    # 我方开球(SET+PLAYING 初期,take_kickoff)
    OPP_KICKOFF = "opp_kickoff"    # 对方开球(避让)
    OUR_SET_PLAY = "our_set_play"  # 我方定位球(任意球/角球/球门球)
    OPP_SET_PLAY = "opp_set_play"  # 对方定位球(避让)
    READY = "ready"                # READY 走位
    STOPPED = "stopped"            # SET(非开球重开) / INITIAL / FINISHED / stopped


def get_phase(context: Context) -> Phase:
    """根据裁判机状态判断当前比赛阶段。"""
    g = context.game
    if g is None:
        return Phase.STOPPED

    state = g.state

    # READY:走 ready 位
    if state == GameState.READY:
        return Phase.READY

    # PLAYING:正常拼抢 or 开球/定位球执行中
    if state == GameState.PLAYING and not g.stopped:
        # 定位球:set_play != NONE,kicking_team 指示哪方
        if g.set_play != SetPlay.NONE and g.kicking_team != KICKING_TEAM_NONE:
            our_team = context.team_id
            if g.kicking_team == our_team:
                return Phase.OUR_SET_PLAY
            else:
                return Phase.OPP_SET_PLAY

        # 开球:secondary_time > 0(倒计时窗口),kicking_team 指示哪方
        if g.secondary_time > 0 and g.kicking_team != KICKING_TEAM_NONE:
            our_team = context.team_id
            if g.kicking_team == our_team:
                return Phase.OUR_KICKOFF
            else:
                return Phase.OPP_KICKOFF

        # 正常拼抢
        return Phase.NORMAL

    # SET / INITIAL / FINISHED / stopped:站定
    return Phase.STOPPED

def get_set_play_type(context: Context) -> SetPlay:
    """当前生效的定位球类型;无定位球(或无裁判机数据)时返回 ``SetPlay.NONE``。

    直接读裁判机的 ``set_play`` 字段,不区分是哪方主罚 —— 哪方由 :func:`get_phase`
    (OUR_SET_PLAY / OPP_SET_PLAY)判定。这里只回答"是什么类型的定位球"。

    共 7 种可能返回值(见 framework.types.SetPlay):
    - ``NONE``:无定位球(正常比赛/开球等)
    - ``DIRECT_FREE_KICK``:直接任意球(可直接射门得分)
    - ``INDIRECT_FREE_KICK``:间接任意球(须先触碰他人才能进球)
    - ``PENALTY_KICK``:点球
    - ``THROW_IN``:界外球(踢入)
    - ``GOAL_KICK``:球门球
    - ``CORNER_KICK``:角球
    """
    g = context.game
    if g is None:
        return SetPlay.NONE
    return g.set_play


# ======================================================================
# Agent 入口
# ======================================================================


class SoccerSimAgent(SoccerAgentMixin, AgentBase):
    """3v3 SoccerSim agent。"""

    player_class = Player

    def init_store(self, store) -> None:
        _log.info("init_store called")
        store.prev_phase = None       # 上一帧 phase,用于检测 phase 跳变(边沿)
        store.cur_phase = None
        store.kickoff_taker = None    # 锁定的开球主罚球员 id(每次进入开球时重选)
        store.normal_attacker = None
        store.defense_guard_id = None
        store.defense_presser_id = None
        store.defense_cover_id = None

    @staticmethod
    def play(context: Context, players: list[Player], store) -> None:
        phase = get_phase(context)
        store.prev_phase = store.cur_phase
        store.cur_phase = phase

        # 画可视化(每帧)
        _analyze_and_draw(context, players, store)

        # 当前 phase 以 label 画在场外。
        from .framework import debugdraw
        g = context.game
        game_state = g.state.value if g is not None else "none"
        set_play = g.set_play.value if g is not None else "none"
        secondary_time = g.secondary_time if g is not None else 0.0
        debugdraw.text(
            0.0, context.field.width / 2.0 + 0.2,
            f"phase={phase.value} state={game_state} set={set_play} secondary={secondary_time:.1f}",
            rgb=(1.0, 1.0, 0.0), ns="phase",
        )

        # 活性自理 + 过滤出本帧可行动的球员。
        # ensure_ready:摔倒起身 / 切 walk 模式(异步,不产生移动);被罚下的也做,
        # 这样解罚后能立刻投入。被罚下或未就绪的不参与分派(也不进角色分配,避免把
        # 动不了的人选成 attacker 导致该帧无人进攻)。
        active: list[Player] = []
        for p in players:
            ready = p.ensure_ready()
            if p.is_penalized:
                p.action = "penalized"     # 罚下:可起身/切模式,但不能移动
                p.stop()
            elif not ready:
                p.action = "fallen" if p.is_fallen else "switching_mode"
            elif p.pose is None:
                p.action = "no_pose"       # 自己位置未知:不参与分派(下游按 pose 已知处理)
                p.stop()
            else:
                active.append(p)

        # 按 phase 对整队分派一次(角色分配等全队计算只在 _act_* 里算一次)。
        if phase == Phase.NORMAL:
            _act_normal(context, active, store)
        elif phase == Phase.OUR_KICKOFF:
            _clear_normal_sticky(store)
            _act_our_kickoff(context, active, store)
        elif phase == Phase.OPP_KICKOFF:
            _clear_normal_sticky(store)
            _act_opp_kickoff(context, active)
        elif phase == Phase.OUR_SET_PLAY:
            _clear_normal_sticky(store)
            _act_our_set_play(context, active, store)
        elif phase == Phase.OPP_SET_PLAY:
            _clear_normal_sticky(store)
            _act_opp_set_play(context, active, store)
        elif phase == Phase.READY:
            _clear_normal_sticky(store)
            _act_ready(context, active)
        elif phase == Phase.STOPPED:
            _clear_normal_sticky(store)
            for p in active:
                p.action = "stopped"
                p.stop()

        # 队员可视化统一在最后画一遍:覆盖所有球员(含判罚/未就绪/STOPPED),
        # 修复 SET 等状态下红球/标签消失的问题。
        for p in players:
            _draw_teammate_marker(p)


def _clear_normal_sticky(store) -> None:
    store.normal_attacker = None
    store.defense_guard_id = None
    store.defense_presser_id = None
    store.defense_cover_id = None


def _clear_defensive_roles(store) -> None:
    """离开防守阵型时清除角色锁定，避免下次防守沿用失效人选。"""
    store.defense_guard_id = None
    store.defense_presser_id = None
    store.defense_cover_id = None


def _player_dist_to_ball(context: Context, p: Player) -> float:
    """球员到球当前位置的距离。"""
    ball = context.ball
    return (
        dist(p.pose.x, p.pose.y, ball.x, ball.y) + _fallen_time_cost(p)
        if ball is not None else math.inf
    )


def _fallen_time_cost(p: Player) -> float:
    return FALLEN_COST if p.is_fallen else 0.0


def _select_closest_attacker(
    context: Context,
    players: list[Player],
    preferred_id: int | None = None,
) -> Player:
    """选到球距离最小的球员。

    ``players`` 非空、已就绪、pose 已知。normal 与开球共用。
    """
    ranked = [(p, _player_dist_to_ball(context, p)) for p in players]
    best, best_dist = min(ranked, key=lambda item: item[1])
    preferred = next((item for item in ranked if item[0].id == preferred_id), None)
    if (
        preferred is not None
        and preferred[1] <= best_dist + ATTACKER_KEEP_DIST_MARGIN_M
    ):
        return preferred[0]
    return best


def _select_sticky_role(
    players: list[Player],
    score,
    preferred_id: int | None,
) -> Player:
    """按 ``score`` 选最低分球员；旧人未明显落后时保留其角色。"""
    ranked = [(p, score(p)) for p in players]
    best, best_score = min(ranked, key=lambda item: item[1])
    preferred = next((item for item in ranked if item[0].id == preferred_id), None)
    if (
        preferred is not None
        and preferred[1] <= best_score + DEFENSE_ROLE_KEEP_MARGIN_M
    ):
        return preferred[0]
    return best


def _defensive_threat(context: Context):
    """选当前最具威胁的对手，供补位者盯防。

    没有“持球人”状态，因此只用公开位置估计：离己方球门更近、同时更接近
    球的对手优先。结果可能为 ``None``，此时补位者退守球的己方一侧。
    """
    ball = context.ball
    if ball is None:
        return None
    gx, gy = own_goal(context)
    candidates = [
        (opponent_id, opponent)
        for opponent_id, opponent in context.opponents.items()
        if opponent.pose is not None
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            dist(item[1].pose.x, item[1].pose.y, gx, gy)
            + DEFENSE_THREAT_BALL_WEIGHT
            * dist(item[1].pose.x, item[1].pose.y, ball.x, ball.y)
        ),
    )


def _cover_target(
    context: Context,
    presser: Player,
    threat,
) -> tuple[float, float] | None:
    """返回补位/盯防点：对手的球门侧，同时与逼抢者拉开距离。"""
    ball = context.ball
    if ball is None:
        return None

    anchor = threat[1].pose if threat is not None else ball
    gx, gy = own_goal(context)
    dx, dy = gx - anchor.x, gy - anchor.y
    length = math.hypot(dx, dy)
    if length < 1e-6:
        ux, uy = -1.0, 0.0
    else:
        ux, uy = dx / length, dy / length

    # 先站到威胁对手的球门侧；再确保站在球的己方一侧，形成补位深度。
    tx = min(
        anchor.x + ux * DEFENSE_COVER_GOAL_SIDE_M,
        ball.x - DEFENSE_COVER_BEHIND_BALL_M,
    )
    ty = anchor.y + uy * DEFENSE_COVER_GOAL_SIDE_M

    # 若补位点会与逼抢者重合，则横向错开，避免两名防守员围到同一点。
    if presser.pose is not None and dist(
        tx, ty, presser.pose.x, presser.pose.y,
    ) < DEFENSE_COVER_MIN_PRESS_GAP_M:
        side = 1.0 if ty <= presser.pose.y else -1.0
        ty += side * DEFENSE_COVER_LATERAL_GAP_M

    return clamp_inside_field(context, tx, ty, margin=DEFENSE_FIELD_MARGIN_M)


def _act_defensive_normal(context: Context, players: list[Player], store) -> None:
    """我方半场防守：一人逼抢、一人补位/盯防、一人守门。"""
    ball = context.ball
    if ball is None or not players:
        return

    gx, gy = own_goal(context)
    guard = _select_sticky_role(
        players,
        lambda p: dist(p.pose.x, p.pose.y, gx, gy),
        getattr(store, "defense_guard_id", None),
    )
    store.defense_guard_id = guard.id
    guard.guard()

    rest = [p for p in players if p is not guard]
    if not rest:
        return

    presser = _select_sticky_role(
        rest,
        lambda p: _player_dist_to_ball(context, p),
        getattr(store, "defense_presser_id", None),
    )
    store.defense_presser_id = presser.id
    presser.action = "defense:press"
    presser.attack()

    rest = [p for p in rest if p is not presser]
    if not rest:
        return

    threat = _defensive_threat(context)
    target = _cover_target(context, presser, threat)
    cover = _select_sticky_role(
        rest,
        lambda p: (
            dist(p.pose.x, p.pose.y, target[0], target[1])
            if target is not None else math.inf
        ),
        getattr(store, "defense_cover_id", None),
    )
    store.defense_cover_id = cover.id
    threat_label = "ball" if threat is None else f"opp{threat[0]}"
    cover.action = f"defense:cover:{threat_label}"
    cover.move_to_position(target)

    # 3v3 正常只有一个剩余补位者；若人数更多，额外球员仍保持原有支援。
    for p in rest:
        if p is not cover:
            p.action = "support"
            p.support()


def _act_normal(context: Context, players: list[Player], store) -> None:
    """NORMAL:我方半场使用防守阵型，其余保持原有攻击/守门/支援分工。

    ``players`` 是本帧可行动球员(已就绪、pose 已知),这里直接挑角色并执行。
    """
    if not players:
        return

    ball = context.ball
    if ball is not None and ball.x <= DEFENSE_ACTIVATE_X:
        store.normal_attacker = None
        _act_defensive_normal(context, players, store)
        return

    _clear_defensive_roles(store)

    attacker = _select_closest_attacker(
        context, players, getattr(store, "normal_attacker", None),
    )
    store.normal_attacker = attacker.id
    attacker.action = "attack"
    attacker.attack()

    # guard:剩下人里离己方门最近者(如还有人)
    rest = [p for p in players if p is not attacker]
    if rest:
        gx, gy = own_goal(context)
        guard = min(rest, key=lambda p: dist(p.pose.x, p.pose.y, gx, gy))
        guard.guard()  
        rest = [p for p in rest if p is not guard]

    # support:其余全部
    for p in rest:
        p.action = "support"
        p.support()


def _act_our_kickoff(context: Context, players: list[Player], store) -> None:
    """OUR_KICKOFF:锁定距离最小者开球,剩下人里离己方门最近者 guard,其余 support。"""
    if not players:
        return

    active_ids = {p.id for p in players}
    if store.prev_phase != Phase.OUR_KICKOFF or store.kickoff_taker not in active_ids:
        # 进入开球阶段，重新选择开球球员
        store.kickoff_taker = _select_closest_attacker(context, players).id

    attacker_id = store.kickoff_taker
    attacker = next((p for p in players if p.id == attacker_id), None)
    if attacker is None:
        return

    attacker.action = "kickoff"
    attacker.kick(0.1, KICK_POWER_OUR_KICKOFF)

    rest = [p for p in players if p is not attacker]
    if rest:
        gx, gy = own_goal(context)
        guard = min(rest, key=lambda p: dist(p.pose.x, p.pose.y, gx, gy))
        guard.guard()
        rest = [p for p in rest if p is not guard]

    for p in rest:
        p.action = "stay"
        p.stop()


def _act_opp_kickoff(context: Context, players: list[Player]) -> None:
    """对方开球:一人守门,两名外场球员在中圈外平行等待。"""
    if not players:
        return
    gx, gy = own_goal(context)
    guard = min(players, key=lambda p: dist(p.pose.x, p.pose.y, gx, gy))
    guard.guard()

    rest = [p for p in players if p is not guard]
    r = context.field.circle_radius
    defender_x = -r - OPP_KICKOFF_DEFENDER_BACK_OFFSET_M
    lateral = OPP_KICKOFF_DEFENDER_LATERAL_OFFSET_M
    slots = [(defender_x, -lateral), (defender_x, lateral)]
    for p, target in zip(rest, slots):
        p.action = "opp_kickoff:ready"
        p.walk_to(target, avoid_ball=True, avoid_robots=True)


def _act_our_set_play(context: Context, players: list[Player], store) -> None:
    """OUR_SET_PLAY:按定位球类型分派, TODO：加入自己的逻辑。默认为 _act_normal"""
    set_play = get_set_play_type(context)
    if set_play == SetPlay.THROW_IN:
        _act_normal(context, players, store)
        return
    if set_play == SetPlay.CORNER_KICK:
        _act_normal(context, players, store)
        return
    if set_play == SetPlay.GOAL_KICK:
        _act_normal(context, players, store)
        return
    _act_normal(context, players, store)


def _act_opp_set_play(context: Context, players: list[Player], store) -> None:
    """对方开球： TODO: 实现自己的逻辑。默认与 Normal 相同"""
    _act_normal(context, players, store)


def _act_ready(context: Context, players: list[Player]) -> None:
    """READY:各自走 ready 位。"""
    game = context.game
    our_kickoff = game is not None and game.kicking_team == context.team_id
    field = context.field
    # 我方开球：优先级站位：(-中圈半径, 0), (我方 goal area 中心点，0)， （0， 中圈半径）
    if our_kickoff:
        if len(players) >= 1:
            p1 = players[0]
            p1.action = "ready"
            p1.walk_to(
                (-field.circle_radius, 0.0),
                face=0.0,
                avoid_ball=True,
                avoid_robots=True,
            )
        if len(players) >= 2:
            p2 = players[1]
            p2.action = "ready"
            p2.walk_to(
                (-field.length / 2.0 + field.goal_area_length, 0.0),
                face=0.0,
                avoid_ball=True,
                avoid_robots=True,
            )
        if len(players) >= 3:
            p3 = players[2]
            p3.action = "ready"
            p3.walk_to(
                (-0.5, field.circle_radius + 2),
                face=0.0,
                avoid_ball=True,
                avoid_robots=True,
            )
    # 对方开球：门将居中；两名外场防守员在中圈外、同一条横线上等待。
    else:
        defender_x = -field.circle_radius - READY_OPP_KICKOFF_CIRCLE_GAP_M
        lateral = READY_OPP_KICKOFF_LATERAL_OFFSET_M
        if len(players) >= 1:
            p1 = players[0]
            p1.action = "ready"
            p1.walk_to(
                (defender_x, -lateral),
                face=0.0,
                avoid_ball=True,
                avoid_robots=True,
            )
        if len(players) >= 2:
            p2 = players[1]
            p2.action = "ready"
            p2.walk_to(
                (-field.length / 2.0 + field.goal_area_length, 0.0),
                face=0.0,
                avoid_ball=True,
                avoid_robots=True,
            )
        if len(players) >= 3:
            p3 = players[2]
            p3.action = "ready"
            p3.walk_to(
                (defender_x, lateral),
                face=0.0,
                avoid_ball=True,
                avoid_robots=True,
            )



# ======================================================================
# 战场可视化 —— 显示球位置 + 球员到球的距离,画到 ROS 可视化
# ======================================================================

def _draw_teammate_marker(p: Player) -> None:
    """我方队员可视化:红色。踢球中→方块,否则→球体。

    每帧对所有球员统一调用(不受 phase/判罚/就绪影响)。标签两行:
    - 上:编号 + 当前高层动作(``p.action``),踢球中追加 ``[KICK]``。
    - 通过形状(方块 vs 球体)再次区分是否进入 kick 状态。
    """
    from .framework import debugdraw

    if p.pose is None:
        return
    red = (1.0, 0.2, 0.2)
    if p.is_kicking:
        debugdraw.cube(p.pose.x, p.pose.y, rgb=red, scale=0.38, ns="teammate")
    else:
        debugdraw.point(p.pose.x, p.pose.y, rgb=red, scale=0.3, ns="teammate")
    kick_tag = " [KICK]" if p.is_kicking else ""
    label = f"{p.id}:{p.action}{kick_tag}"
    debugdraw.text(p.pose.x, p.pose.y, label, rgb=(1.0, 0.9, 0.6), ns="teammate_id")


def _analyze_and_draw(context: Context, players: list[Player], store) -> None:
    """每帧:计算球员到球的距离,画可视化。

    不再依赖 analysis 模块;距离改为基于球当前位置。
    """
    from .framework import debugdraw

    ball = context.ball

    # 球不可见:无可视化
    if ball is None:
        return

    # 1. 画球当前位置(绿色点)
    debugdraw.point(ball.x, ball.y, rgb=(0.0, 1.0, 0.0), scale=0.2, ns="ball_current")

    # 2. 球员到球的距离:我方(红标签)+ 敌方(蓝标签)
    for p in players:
        if p.pose is None:
            continue
        d = dist(p.pose.x, p.pose.y, ball.x, ball.y)
        debugdraw.text(
            p.pose.x + 0.3, p.pose.y - 0.3, f"{d:.1f}m",
            rgb=(1.0, 0.6, 0.6), ns="dist_ours",
        )
    for r in context.opponents.values():
        if r.pose is None:
            continue
        d = dist(r.pose.x, r.pose.y, ball.x, ball.y)
        debugdraw.text(
            r.pose.x + 0.3, r.pose.y - 0.3, f"{d:.1f}m",
            rgb=(0.6, 0.6, 1.0), ns="dist_opp",
        )

