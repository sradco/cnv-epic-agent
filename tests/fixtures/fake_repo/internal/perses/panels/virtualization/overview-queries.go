package virtualization

const overviewTotalClusters = `count(kubevirt_hyperconverged_operator_health_status{cluster=~"$cluster"})`

const overviewTotalVMs = `sum(count(kubevirt_vm_info{cluster=~"$cluster"}) by (name))`

const overviewCPUUsage = `sum(rate(kubevirt_vmi_cpu_usage_seconds_total{cluster=~"$cluster"}[5m]))`
