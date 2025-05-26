.ONESHELL:
.PHONY: uninstall
.PHONY: install


uninstall:
	conda env remove -n quants-lab -y

install:
	conda env create -f environment.yml
# Build local image
build:
	docker build -t hummingbot/quants-lab -f Dockerfile .
# Run db containers
run-db:
	docker compose -f docker-compose-db.yml up -d

# Run db containers
build-datac:
	docker compose -f docker-compose-hbot.yml up -d --build

# down datac containers
down-datac:
	docker compose -f docker-compose-hbot.yml down

# Stop db containerss
stop-db:
	docker compose -f docker-compose-db.yml down

# Run task runner with specified config
run-task:
	TASK_CONFIG=config/$(config) docker compose -f docker-compose-task-runner.yml up task-runner

# Run task runner dettached with specified config
run-task-d:
	TASK_CONFIG=config/$(config) docker compose -f docker-compose-task-runner.yml up task-runner -d

# Stop task runner
stop-task:
	docker compose -f docker-compose-task-runner.yml down
