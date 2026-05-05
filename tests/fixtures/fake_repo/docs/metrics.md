# KubeVirt components metrics

| Operator Name | Name | Kind | Type | Description |
|----------|------|------|------|-------------|
| kubevirt | `kubevirt_vmi_info` | Metric | Gauge | Information about VirtualMachineInstances. |
| kubevirt | `kubevirt_vmi_cpu_usage_seconds_total` | Metric | Counter | Total CPU time spent in all modes. |
| containerized-data-importer | `kubevirt_cdi_import_progress_total` | Metric | Counter | The import progress in percentage |
| kubevirt | `node:kubevirt_vmi_phase:sum` | Recording rule | Gauge | Sum of VMIs per phase and node. |
