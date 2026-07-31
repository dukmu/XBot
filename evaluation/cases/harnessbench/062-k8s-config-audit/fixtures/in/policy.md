# Storefront Kubernetes policy

- Workload containers must define CPU and memory requests and limits.
- Every HTTP workload must define readinessProbe and livenessProbe.
- Service selectors must match the Deployment pod template labels.
- Service targetPort must match a declared containerPort.
- Images must use pinned immutable tags; `latest` is forbidden.
- Privileged containers and privilege escalation are forbidden.
- Public exposure through NodePort is forbidden for checkout-api; use ClusterIP unless a separate ingress approval exists.
