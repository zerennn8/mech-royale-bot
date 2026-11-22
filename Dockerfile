# Mech Royale bot (mechfight/1). Standard library only; map nav data pre-baked in nav.bin.
FROM python:3.13.9-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /bot
COPY mechfight.py world.py bot.py nav.bin ./
RUN python3 -m compileall -q /bot
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
