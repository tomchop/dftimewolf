#!/usr/bin/env bash
#
# This file is generated by l2tdevtools update-dependencies.py any dependency
# related changes should be made in dependencies.ini.

# Exit on error.
set -e

sudo add-apt-repository ppa:gift/dev -y
sudo apt-get update -qq
sudo apt-get install -y python3-pip
sudo apt-get install --reinstall python3-apt
python3 --version
/usr/bin/python3 -m pip install poetry --break-system-packages


if [[ "$*" =~ "include-docker" ]]; then
    # follow instructions at https://docs.docker.com/engine/install/debian/
    echo "Removing conflicting packages"
    for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do sudo apt-get remove $pkg; done

    echo "adding docker key"
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc

    echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
        $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
        sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update

    echo "install docker packages"
    sudo apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

if [[ "$*" =~ "include-grr" ]]; then
    # Start the GRR server container.
    echo "Running include-grr"
    mkdir ~/grr-docker
    echo "Running the GRR docker container"
    sudo docker run \
      --name grr-server -v ~/grr-docker/db:/var/grr-datastore \
      -v ~/grr-docker/logs:/var/log \
      -e EXTERNAL_HOSTNAME="localhost" \
      -e ADMIN_PASSWORD="admin" \
      --ulimit nofile=1048576:1048576 \
      -p 127.0.0.1:8000:8000 -p 127.0.0.1:8080:8080 \
      -d grrdocker/grr:release grr

    echo "Sleeping 180 seconds"
    # Wait for GRR to initialize.
    /bin/sleep 180

    # Install the client.
    echo "Installing GRR client on $NODE_NAME"
    sudo docker cp grr-server:/usr/share/grr-server/executables/installers .
    if sudo dpkg -i installers/*amd64.deb; then
        echo "GRR client installed successfully"
    else
        echo "GRR client installation failed"
        exit 1
    fi
fi

if [[ "$*" =~ "include-timesketch" ]]; then
    # Start the Timesketch server container.
    export PLASO_PPA_TRACK="stable"
    export OPENSEARCH_VERSION="2.9.0"
    echo "Cloning Timesketch from Github"
    git clone https://github.com/google/timesketch.git
    cd timesketch
    cd docker
    cd e2e
    echo "Running the Timesketch docker-compose script"
    sudo -E docker-compose up -d
    # Wait for Timesketch to initialize
    echo "Sleeping 300 seconds..."
    /bin/sleep 300
    cd ../../..
    echo "Credentials for e2e tests are set in https://github.com/google/timesketch/blob/master/docker/e2e/docker-compose.yml"
fi

if [[ "$*" =~ "include-plaso" ]]; then
    echo "Installing plaso"
    docker pull log2timeline/plaso:latest
fi

# pending resolution of https://github.com/log2timeline/l2tdevtools/issues/595
if [[ "$*" =~ "include-turbinia" ]]; then
    echo "Installing Turbinia"
    /usr/bin/python3 -m pip install turbinia
fi

echo "Installing dftimewolf requirements via Poetry"
# Install dftimewolf's pinned requirements
poetry install
