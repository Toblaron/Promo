DC  := docker compose
RUN := $(DC) run --rm promoter

.PHONY: up down logs ps sync top growth promo report cli test lint

up: data .env              ## build + start the watch daemon
	$(DC) up -d --build

down:
	$(DC) down

logs:
	$(DC) logs -f --tail=100

ps:
	$(DC) ps

sync: data .env            ## snapshot stats now
	$(RUN) sync

top: data .env             ## make top BY=gain N=5
	$(RUN) top --by $(or $(BY),views) -n $(or $(N),10)

growth: data .env          ## make growth DAYS=30
	$(RUN) growth --days $(or $(DAYS),7)

promo: data .env           ## make promo ARGS="--top 3 --by gain"
	$(RUN) promo $(ARGS)

report: data .env
	$(RUN) report --days $(or $(DAYS),7)

cli: data .env             ## make cli ARGS="promo --video https://youtu.be/ID --kind spotlight"
	$(RUN) $(ARGS)

test:                      ## run the suite in a throwaway container (no API key needed)
	docker run --rm -v "$(CURDIR)":/src -w /src python:3.13-slim \
	  sh -c "pip install -q --root-user-action=ignore pytest && python -m pytest -q -p no:cacheprovider"

lint:
	docker run --rm -v "$(CURDIR)":/src -w /src ghcr.io/astral-sh/ruff:0.6.9 check .

data:
	mkdir -p data

.env:
	@cp .env.example .env && echo "Created .env, set YT_API_KEY and re-run." && exit 1
