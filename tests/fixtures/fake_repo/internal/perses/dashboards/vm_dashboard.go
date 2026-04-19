package dashboards

import (
	"github.com/perses/perses/go-sdk/dashboard"
)

func SingleVMDashboard() *dashboard.Builder {
	return dashboard.New("Single VM Overview")
}
