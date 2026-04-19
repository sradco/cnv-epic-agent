package virtualization

import (
	"github.com/perses/perses/go-sdk/dashboard"
)

func BuildVirtOverview(project string, datasource string) (dashboard.Builder, error) {
	return dashboard.New("acm-virt-overview",
		dashboard.ProjectName(project),
		dashboard.Name("Virtualization / Clusters Overview"),
	)
}
