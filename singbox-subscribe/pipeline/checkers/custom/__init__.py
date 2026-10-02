"""Свои проверяющие алгоритмы.

Положите сюда .py-файл с классом-наследником Checker — он подхватится
автоматически и появится в ``python -m pipeline checkers`` и в выборе
PIPELINE_CHECKERS.

Минимальный пример (см. также example_ping.py):

    from pipeline.checkers import CheckOutcome, CheckResult, Checker

    class MyChecker(Checker):
        name = "my_checker"
        description = "Моя проверка"
        decides_availability = True

        async def check(self, ctx):
            outcomes = {}
            for line in ctx.lines:
                ok, ping = await ctx.run_sync(probe, line)
                outcomes[line] = CheckOutcome(ok=ok, ping_ms=ping)
            return CheckResult(outcomes)

Папку можно переопределить переменной PIPELINE_CUSTOM_CHECKERS_DIR.
"""
