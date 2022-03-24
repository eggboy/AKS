# Setting up Docker Registry Mirror with containerd

**This requires containerd version >= 1.5 as a minimum.** Docker Registry image(docker.io/registry) provides several authentication options like token based, htpasswd and none. Example here is using **none** as assuming this is to run only inside the private network. Also, there is no way to specify the basic auth credentials with containerd mirror configuration at the moment. https://github.com/containerd/containerd/issues/6438

## Create Registry mirror

Change ingress settings accordingly. I'm using cert-manager for SSL certificate in my example. 

```
$ kubectl apply -f registry.yml
$ kubectl apply -f ingress.yml
```

One ingress and deployments are running, access the url to see if it can be accessed. Ex. https://registry.jaylee.cloud/v2/_catalog

## Create DaemonSet to install hosts.toml

We will be using DS to install certificates and hosts.toml for mirror configuration. 

```
$ kubectl apply -f containerd_hosts_ds.yml
```