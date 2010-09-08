#!/bin/bash

# Skywriting deployment script

# usage: sw-deploy.sh hostname privkey [sw-user] [sw-user-pw]

TARGETHOST=$1
PRIVKEY=$2

if [[ $1 == '' || $2 == '' ]]; then
    echo "usage sw-deploy.sh hostname privkey [sw-user] [sw-user-pw]"
    exit 0
fi

if [[ $3 == '' ]]; then
    SWUSER='root'
else
    SWUSER=$3
fi

if [[ $4 == '' ]]; then
    SWUSERPW='wduw2g2d'
else
    SWUSERPW=$4
fi

# output
echo "Deploying to $TARGETHOST..."

# install private key
#ssh-copy-id -i $PRIVKEY $SWUSER@$TARGETHOST
echo y | plink -pw $SWUSERPW $SWUSER@$TARGETHOST 'mkdir .ssh'
pscp -q -pw $SWUSERPW $PRIVKEY.pub $SWUSER@$TARGETHOST:.ssh/authorized_keys

# run remote deployment script
scp -o StrictHostKeyChecking=no -q -i $PRIVKEY sw-deploy-local.sh $SWUSER@$TARGETHOST:
ssh -o StrictHostKeyChecking=no -f -i $PRIVKEY $SWUSER@$TARGETHOST "~$SWUSER/sw-deploy-local.sh 1>&2 2>/dev/null"

# output
echo "done!"
