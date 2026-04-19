package alerts

import (
	promv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
)

var cdiAlerts = []promv1.Rule{
	{
		Alert: "CDIDataImportCronOutdated",
		Expr:  intstr.FromString("cdi_dataimportcron_outdated == 1"),
		For:   ptr.To(promv1.Duration("15m")),
		Labels: map[string]string{
			"severity": "warning",
		},
	},
}
