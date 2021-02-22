#!/bin/sh -e

# deploy test app
kubectl run echoserver --image=k8s.gcr.io/echoserver:1.4

# render an type LB service
kubectl expose pod echoserver --name=echoserver-lb --port=80 --target-port=8080 --type=LoadBalancer
kubectl get svc echoserver-lb

# ensure client source IP is maintained (instead of host SNAT)
kubectl patch svc echoserver-lb -p '{"spec":{"externalTrafficPolicy":"Local"}}'

