# Lab browser traffic

`browser-traffic.yaml` makes the read-only requests a person browsing the
AKS Store would make (`GET` pages, product lists and the order queue), every
15 seconds, through the `store-front` and `store-admin` nginx proxies.

Why: the lab's traffic generators (`virtual-customer`, `virtual-worker`)
call the back-end services directly, so the two web front ends never call
anything and live telemetry can't see them. A release whose radius includes
them is then held as `live_uncovered`. In production, real users produce
this traffic. Here it is generated instead of exempting the front ends as
silent services, so the lab exercises the same path a real cluster does.

- Lab only; never part of what a customer installs.
- Runs in namespace `lab-traffic`, which Beyla doesn't instrument and
  onboarding doesn't scan, so the generator itself never appears as a
  service.
- `GET` only: it never places, changes or completes orders.
- Applied by `.claude/skills/app-up/lab.sh up`; `kubectl logs -n
  lab-traffic deploy/browser-traffic` shows each request's status code.
