FROM python:3.14-trixie

WORKDIR /bot

COPY . .

RUN python -m venv .venv
RUN .venv/bin/pip install .

CMD [ ".venv/bin/python", "-m", "main" ]