USERNAME := $(shell echo $(USER) | sed -r 's/[0-9]//g' | cut --complement -c 1-3)
CURRENT_DIR := $(notdir $(CURDIR))
DEV_NAME := $(USERNAME)_$(word $(words $(subst /, ,$(CURRENT_DIR))), $(subst /, ,$(CURRENT_DIR)))
#PROD_NAME := $(shell echo $(word $(words $(subst /, ,$(CURRENT_DIR))), $(subst /, ,$(CURRENT_DIR))) | tr '[:upper:]' '[:lower:]')
#BRANCH := $(shell git branch --show-current)
#PROD_NAME := $(shell git branch --show-current)
PROD_NAME := $(USERNAME)_$(CURRENT_DIR)_$(shell git branch --show-current)
DEV_NAME := $(USERNAME)_$(PROD_NAME)_dev
MAKEFLAGS += --silent
SHELL := /bin/bash

# Colors for echos 
ccend = $(shell tput sgr0)
ccbold = $(shell tput bold)
ccgreen = $(shell tput setaf 2)
ccso = $(shell tput smso)

test:
	echo Prefix: $(USERNAME)
	echo Repo Name: $(CURRENT_DIR)
	echo Dev-Container Name prefix: $(DEV_NAME)
	echo Prod-Container Name: $(PROD_NAME)

docker-build:
	docker build --secret id=netrc,src=.netrc --build-arg git_name=$(GIT_NAME) --build-arg git_email=$(GIT_EMAIL) -t $(PROD_NAME)_deploy -f Dockerfile . 
	#docker build --secret id=netrc,src=.netrc -t $(PROD_NAME) -f Dockerfile . 

docker-build-no-cache:
	docker build --secret id=netrc,src=.netrc --build-arg git_name=$(GIT_NAME) --build-arg git_email=$(GIT_EMAIL) -t $(PROD_NAME)_deploy --no-cache -f Dockerfile .
	#docker build --secret id=netrc,src=.netrc -t $(PROD_NAME) --no-cache -f Dockerfile .

docker-build-dev:
	docker build --secret id=netrc,src=.netrc --build-arg git_name=$(GIT_NAME) --build-arg git_email=$(GIT_EMAIL) -t $(DEV_NAME) --file Dockerfile_dev .

docker-build-dev-no-cache:
	docker build --no-cache --secret id=netrc,src=.netrc --build-arg git_name=$(GIT_NAME) --build-arg git_email=$(GIT_EMAIL) -t $(DEV_NAME)_deploy_dev --file Dockerfile_dev .

start-bash:
	docker run --memory=32g --memory-swap=40g --name $(PROD_NAME) --rm -ti --env-file ./.env --mount type=bind,source="$(PWD)"/testing,target=/app/$(PROD_NAME)/testing/ $(PROD_NAME)_deploy sh -c 'bash'

start-bash-dev:
	docker run --memory=32g --memory-swap=40g --name $(DEV_NAME) -ti --rm --env-file ./.env --mount type=bind,source="$(PWD)",target=/app/$(PROD_NAME) \
	$(DEV_NAME) sh -c 'bash'

start-jupyter:
	docker run --name $(DEV_NAME) --rm --env-file ./.env --detach -p 20001:8887 --kernel-memory 0.5g -m 32g --volume .:/app ${DEV_NAME} jupyter lab --port=8887 --ip="0.0.0.0" --allow-root --no-browser --NotebookApp.token=f9a6a4420774ed08b734749254abf91b7c2f4def9bea9659 &

show-jupyter:
	docker exec ${DEV_NAME} jupyter server list
