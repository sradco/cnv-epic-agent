package recordingrules

import (
	"github.com/machadovilaca/operator-observability-toolkit/pkg/operatormetrics"
	"github.com/machadovilaca/operator-observability-toolkit/pkg/operatorrules"
	"k8s.io/apimachinery/pkg/util/intstr"
)

var operatorRecordingRules = []operatorrules.RecordingRule{
	{
		MetricsOpts: operatormetrics.MetricOpts{
			Name: "kubevirt_hyperconverged_operator_health_status",
			Help: "Indicates whether HCO health status is healthy (0), warning (1) or critical (2)",
		},
		MetricType: operatormetrics.GaugeType,
		Expr:       intstr.FromString("max(kubevirt_hco_system_health_status)"),
	},
	{
		MetricsOpts: operatormetrics.MetricOpts{
			Name: "cluster:vmi_request_cpu_cores:sum",
			Help: "Sum of CPU core requests for all running virt-launcher VMIs",
		},
		MetricType: operatormetrics.GaugeType,
		Expr:       intstr.FromString(`sum(kube_pod_container_resource_requests{resource="cpu"})`),
	},
}
