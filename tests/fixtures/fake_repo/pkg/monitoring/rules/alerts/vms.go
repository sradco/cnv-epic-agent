package alerts

import (
	promv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
)

var vmsAlerts = []promv1.Rule{
	{
		Alert: "VirtLauncherPodsStuckFailed",
		Expr:  intstr.FromString("sum(kube_pod_status_phase{phase='Failed', pod=~'virt-launcher-.*'}) >= 200"),
		For:   ptr.To(promv1.Duration("10m")),
		Annotations: map[string]string{
			"summary": "At least 200 virt-launcher pods are stuck in Failed state.",
		},
		Labels: map[string]string{
			"severity":               "critical",
			"operator_health_impact": "critical",
		},
	},
	{
		Alert: "VMCannotBeEvicted",
		Expr:  intstr.FromString("kubevirt_vmi_non_evictable == 1"),
		For:   ptr.To(promv1.Duration("1m")),
		Labels: map[string]string{
			"severity": "warning",
		},
	},
}
