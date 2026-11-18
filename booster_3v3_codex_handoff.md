# Booster 3v3 Soccer — Codex Handoff

**Scope:** Project context and previously discussed proposals only. **No implementation is requested in this handoff.**
**Reference date:** 2026-11-17. Official repository `main` inspected; the exact local checkout/commit and any subsequent modifications are not independently verified.

## 1. Competition and platform

- **Event:** 第二届北京市大学生“人工智能+”创新大赛 — 绿茵智竞（人形机器人足球多智能体协同决策与自主对抗）. Three autonomous robots per team; cloud-simulated football; no physical robots required; standard match lasts **10 minutes**.
- **Tools:** **Booster Studio** for Agent build/deploy, virtual-robot simulation and visual inspection; **Docker** supplies simulation/runtime containers. Python source can be edited in VS Code with Codex. Official framework uses ROS/SDK interfaces internally.
- **BIT internal selection (校赛):** Five-minute narrated PPT due **2026-11-20**; judging **Nov 21–23**; up to five teams per topic announced by **Nov 25**. Evaluation includes 赛题理解 20, 项目规划与方案可行性 25, 当前进度与阶段性成果 25, 创新性与应用价值 10, 规则契合度 15, 合规与原创性 5.
- **Official enterprise competition:** Qualification **Nov 16–30**, Swiss preliminaries **Dec 1–2** (top 32), groups **Dec 3–4** (top 16), knockout rounds **Dec 5** (top four final result for this event).
- **Qualification:** `.agent` is submitted through the event website's **我的队伍** page. The team must beat the **official preset AI at least once by Nov 30**. Failed attempts can be retried after strategy updates as scheduled. The organizer's GitHub sample Agent is **starter code, not the official qualification opponent**. Team had no qualification-platform submission account as of the conversation; local tests are not official qualification results.
- **Match constraints:** A deployed strategy autonomously decides actions; its code cannot be replaced mid-match. Normal-play goals **do not require a prior pass**. Kick-off scoring has additional touch/expiration restrictions; other set pieces have special rules. During opponent restarts, required distance is typically **1.45 m**. Illegal positioning, interference, prolonged falls/inactivity, etc. can result in **30-second penalties**. Only officially documented robot/agent interfaces may be used; changing physics, robot hardware abilities, match state or accessing hidden simulator/referee interfaces is prohibited.
- **Booster Learn community:** `开发第一个 Agent`, `入门 Booster Studio`, and the **14-part 仿真足球赛专题课**, including code architecture, FSM, `Context`, `Player`, `store`, parameter tuning, set-piece attack/defense, zone tactics, movement/avoidance, FAQs and rules. Strategy decisions refresh at approximately **30 Hz**.

## 2. Repository / working environment

- **Official starter:** https://github.com/BoosterRobotics/sim-3v3-simple-framework
- **Personal GitHub repository:** https://github.com/ychew247/3v3_football_agent (private repository creation succeeded; initial `origin` remote addition failed because `origin` already existed; successful push was not confirmed).
- **Local Windows folder:** `C:\sim-3v3-simple-framework`; PowerShell, Git, Booster Studio, Docker Desktop/WSL2, VS Code/Codex.
- **Relevant files:**
  - `src/main.py` — top-level `Phase` state machine, `play(context, players, store)`, match-phase tactics and role allocation.
  - `src/player.py` — per-robot `Player` actions, movement, shooting, guard/support behavior, recovery.
  - `src/param.py` — strategy constants for kick strength, movement speeds/gains, distances and planning.
  - `src/utils/` — geometry, obstacles, global A* and local obstacle avoidance.
  - `src/framework/` — official 30 Hz runtime, `Context`, ROS input, robot SDK backend, referee codec; platform integration layer.
  - `agent.toml`, `build.toml`, `.booster-studio/project.json` — Agent metadata, packaging and Booster Studio scene settings.
- **Prior environment issue:** A virtual-robot activation attempt timed out waiting for a local physics WebSocket (`ws://127.0.0.1:<port>`), causing deployment failure. Pylance also flagged unavailable host-side `booster_agent_framework` imports and possible `None` poses. These editor warnings did not establish the deployment failure's cause. Subsequent local match observation was reported, but the precise activation fix was not documented.

## 3. Existing baseline behavior (official `main`, as inspected)

- **Per-frame pipeline:** ROS/referee/robot data → `Context` snapshot → `get_phase()` → `play()` → available-player filtering (`ensure_ready`, fallen/penalized/unknown-pose handling) → phase-specific `_act_*` → `Player` commands → SDK backend. `store` retains state across frames.
- **Match phases:** `NORMAL`, `OUR_KICKOFF`, `OPP_KICKOFF`, `OUR_SET_PLAY`, `OPP_SET_PLAY`, `READY`, `STOPPED`.
- **Normal play:** Nearest eligible robot to ball becomes **attacker**; among remaining robots, nearest to own goal becomes **guard**; remaining robot(s) **support**. Attacker selection already has a **0.3 m retention margin** to reduce rapid switches. Guard is a **dynamic role**, not permanently assigned to a numbered robot.
- **`Player.attack()`:** Approaches a point behind the ball and enters sustained kick mode when within **2.0 m**, leaving it beyond **2.5 m** (hysteresis). `plan_kick()` normally aims toward the **opponent goal center**, using default kick strength. No complete pass-vs-dribble-vs-shoot decision system is present.
- **`Player.guard()`:** Moves toward a **fixed own-goal-area center** and faces the ball; it does not yet actively slide along the goalmouth in response to ball lateral position.
- **`Player.support()`:** Primarily occupies a defensive support point on the **ball-to-own-goal line** (default ~3 m from ball), not a dedicated forward passing lane or opponent-marking assignment.
- **Kick-off:** One nearest robot is selected/retained as taker; another guards; others wait. Opponent kick-off uses defensive fixed waiting slots. `READY` sets starting positions; `STOPPED` stops movement.
- **Set pieces:** `OUR_SET_PLAY` distinguishes throw-in, corner and goal kick but currently falls back to `_act_normal()`; `OPP_SET_PLAY` also falls back to normal tactics. No fully specialized set-piece teamwork is implemented.
- **Parameters:** Kick command power is clamped to **1–10** (default **5**); example movement command ceilings are `MAX_LINEAR = 2.0 m/s`, `MAX_ANGULAR = 2.0 rad/s`. These are strategy command limits, **not modifiable robot physical abilities**. There are no inherent individual-player stats such as “passing skill,” “shooting accuracy,” or physical “kick strength.” Role-specific command profiles are a proposed strategy feature, not existing player attributes.

## 4. Observed robot behavior and discussed solutions (4.1)

| Observed limitation (local simulation) | Discussed solution / desired behavior — **not implemented** |
|---|---|
| **Attack decisions:** Under pressure, tends to kick the ball away; no reliable possession, dribbling or passing. | Situational choice between a safe pass, controlled advance, shot or clearance; assess teammate position, passing lane and receiving space; coordinated runs to open space. |
| **Shooting:** Shots sometimes occur from poor range/angle or away from goal. | Shot gating using goal distance, angle and nearby defenders; aim for open sections of goal away from keeper rather than always center. |
| **Defense:** One defender does not press the ball carrier; the second fails to mark/cover; multiple robots may crowd the ball. | **One presses, one covers/marks, keeper protects goal**; conditional double-team only where coverage remains intact. |
| **Goalkeeping:** Keeper mostly remains central and fails to cover lateral ball movement. | Controlled side-to-side repositioning based on ball position and probable shot path; conservative off-line movements/clearances. |
| **Loose-ball recovery:** Some robots stand still rather than challenge an unclaimed ball. | Prefer nearest *suitable* chaser, using time-to-ball, ball trajectory and defensive risk; others support/guard; safe positioning/body blocking without deliberate collisions. |
| **Movement/collisions:** Slow pursuit; collisions with teammates/opponents lead to falls and occasional removal/penalties. | Tune speed/turn commands conservatively; better route selection, robot-aware avoidance and slowing in crowded areas. |
| **Inconsistent outcomes:** Different local matches have substantially different scores. | Compare repeated matches under equivalent conditions; record goals for/against, results, falls, penalties and other observable behavior; distinguish evidence from anecdotal variance. |

## 5. Source-level strategic gaps and discussed solutions (4.2)

| Identified code limitation | Discussed solution / desired behavior — **not implemented** |
|---|---|
| **Simple role selection:** Mainly ball/goal distance, without full threat, formation or ball-motion evaluation. | Combine distance with time-to-ball, opponent threat, ball trajectory, teammate positions and defensive risk. Enhance the **existing** attacker role-retention logic; nearest-to-ball remains a useful default, not an absolute rule. |
| **Incomplete set-piece tactics:** Corner, goal kick and throw-in largely reuse normal-play logic. | Distinct set-piece behavior; choose a suitable available taker, with coordinated receiving/coverage positions and rule-aware timing/distances. No penalty-kick feature is part of the current proposal. |

**Set-piece role-profile concept:** Each robot has the same underlying physical capabilities, but a role can carry predeclared **software command preferences** (kick power/direction, movement behavior and action-selection rules). Profiles can be chosen dynamically when a robot's role changes **during a match**, without changing deployed code. Passing ability/accuracy is **not an SDK-provided individual attribute**; it would be defined through decision and control logic.

- **Corner kick:** Taker with suitable passing profile; teammates move into receiving/scoring positions; pass direction/power/lane/target are strategic calculations.
- **Goal kick:** Goalkeeper or suitably placed available taker, with safe short distribution or stronger clearance according to pressure.
- **Throw-in:** Suitable nearby taker, teammates locate free receiving space; short distribution intended to retain possession.
- **Rule compliance:** Restart-specific position, distance, touch and time limits remain constraints.

## 6. Testing evidence and development status

- The unmodified baseline was observed in local simulation; user reported roughly **five minutes** of behavioral observations. No reliable controlled multi-match statistics or verified improvement scores were supplied.
- Team's BIT submission section is **当前进度与阶段性成果**: records development environment, courses studied, current prototype, genuine screenshots/logs, observed limitations and test evidence. Team members handle the main narrated PPT.
- Previously discussed candidate innovation themes (not demonstrated results): **pass-opportunity scoring / decision-making**, **multi-factor dynamic roles**, **role-adaptive software profiles**, **set-piece teamwork**.
- There is **no confirmed official preset-AI victory** and **no confirmed completed implementation** of the proposed upgrades in this conversation.

## Sources

- Official framework and behavior: [repository](https://github.com/BoosterRobotics/sim-3v3-simple-framework), [AGENTS.md](https://github.com/BoosterRobotics/sim-3v3-simple-framework/blob/main/AGENTS.md), [`main.py`](https://github.com/BoosterRobotics/sim-3v3-simple-framework/blob/main/src/main.py), [`player.py`](https://github.com/BoosterRobotics/sim-3v3-simple-framework/blob/main/src/player.py), [`param.py`](https://github.com/BoosterRobotics/sim-3v3-simple-framework/blob/main/src/param.py).
- Competition portal: https://bjcsai2.booster.tech
- Organizer's materials previously supplied in conversation: **《第二届北京市大学生“人工智能+”创新大赛-绿茵智竞 秩序册》**, **《关于组织开展第二届北京市大学生“人工智能+”创新大赛北京理工大学校内选拔的通知》**, and teammate's **《足球赛.docx》**. Their details are summarized above, but the attachment contents themselves are not embedded in this file.
