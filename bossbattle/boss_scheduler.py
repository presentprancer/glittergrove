################################################################################
# FILE: cogs/boss_scheduler.py — background loops (weakness rotate + idle heal)
################################################################################

import random
from datetime import timedelta

from discord.ext import commands, tasks

from cogs.worldboss.settings import (
    HOME_GUILD_ID,
    ROTATE_MINUTES as BOSS_WEAKNESS_ROTATE_MINUTES,
    IDLE_HEAL_MINUTES as BOSS_IDLE_HEAL_MINUTES,
    IDLE_HEAL_PCT as BOSS_IDLE_HEAL_PCT,
    WEAKNESS_ORDER,
)
from cogs.worldboss.storage import load_boss, save_boss
from cogs.worldboss.schema import _now
from cogs.worldboss.util import _parse_iso
from cogs.worldboss.sync import boss_state_lock
from cogs.worldboss.shields import _shield_active


class BossScheduler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not self.rotate_weakness.is_running():
            self.rotate_weakness.start()
        if not self.boss_idle_regen.is_running():
            self.boss_idle_regen.start()
        if not self.shield_cleaner.is_running():
            self.shield_cleaner.start()

    # Weakness rotation
    @tasks.loop(minutes=BOSS_WEAKNESS_ROTATE_MINUTES)
    async def rotate_weakness(self):
        try:
            async with boss_state_lock:
                b = await load_boss()
                if int(b.get("hp", 0)) <= 0 or b.get("kill_handled"):
                    return
                cur = (b.get("weakness") or "").lower()
                try:
                    i = WEAKNESS_ORDER.index(cur)
                    new = WEAKNESS_ORDER[(i + 1) % len(WEAKNESS_ORDER)]
                except Exception:
                    new = random.choice(WEAKNESS_ORDER)
                b["weakness"] = new
                b["last_rotate"] = _now().isoformat()
                await save_boss(b)
        except Exception:
            pass

    # Idle heal when no actions in the last N minutes
    @tasks.loop(minutes=max(1, BOSS_IDLE_HEAL_MINUTES))
    async def boss_idle_regen(self):
        try:
            if BOSS_IDLE_HEAL_MINUTES <= 0 or BOSS_IDLE_HEAL_PCT <= 0:
                return
            async with boss_state_lock:
                b = await load_boss()
                if int(b.get("hp", 0)) <= 0 or b.get("kill_handled"):
                    return
                cutoff = _now() - timedelta(minutes=BOSS_IDLE_HEAL_MINUTES)
                recent = [a for a in (b.get("last_actions") or []) if _parse_iso(a.get("ts")) and _parse_iso(a.get("ts")) > cutoff]
                if recent:
                    return
                mx = max(1, int(b.get("max_hp", 1)))
                heal = max(1, int(mx * BOSS_IDLE_HEAL_PCT))
                b["hp"] = min(mx, int(b.get("hp", 0)) + heal)
                await save_boss(b)
        except Exception:
            pass

    # Clean expired shields
    @tasks.loop(seconds=15)
    async def shield_cleaner(self):
        try:
            async with boss_state_lock:
                b = await load_boss()
                s = b.get("shield")
                if not s:
                    return
                active = _shield_active(b)
                if active:
                    exp = _parse_iso(s.get("expires"))
                    if exp and _now() < exp:
                        return
                b["shield"] = None
                await save_boss(b)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(BossScheduler(bot))


if __name__ == "__main__":
    # Simple smoke test for import-time errors
    print("boss_scheduler.py loaded OK (no runtime executed).")
