package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// EDIT THIS FILE!  THIS IS SCAFFOLDING FOR YOU TO OWN!
// NOTE: json tags are required.  Any new fields you add must have json tags for the fields to be serialized.

// MemcachedSpec defines the desired state of Memcached
type MemcachedSpec struct {
	// Size 是期望的 memcached 实例数。
	// 用 OpenAPI v3 校验标记把取值范围钉死在 API server 一侧：
	// 非法值在 kubectl apply 时就会被拒绝，而不是等控制器创建 Deployment 才失败。
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=3
	// +kubebuilder:default=1
	// +optional
	Size int32 `json:"size,omitempty"`
}

// MemcachedStatus defines the observed state of Memcached.
type MemcachedStatus struct {
	// For Kubernetes API conventions, see:
	// https://github.com/kubernetes/community/blob/master/contributors/devel/sig-architecture/api-conventions.md#typical-status-properties

	// conditions represent the current state of the Memcached resource.
	// Each condition has a unique type and reflects the status of a specific aspect of the resource.
	//
	// Standard condition types include:
	// - "Available": the resource is fully functional
	// - "Progressing": the resource is being created or updated
	// - "Degraded": the resource failed to reach or maintain its desired state
	//
	// The status of each condition is one of True, False, or Unknown.
	// +listType=map
	// +listMapKey=type
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Size",type="integer",JSONPath=".spec.size"
// +kubebuilder:printcolumn:name="Available",type="string",JSONPath=".status.conditions[?(@.type==\"Available\")].status"
// +kubebuilder:printcolumn:name="Age",type="date",JSONPath=".metadata.creationTimestamp"

// Memcached is the Schema for the memcacheds API
type Memcached struct {
	metav1.TypeMeta `json:",inline"`

	// metadata is a standard object metadata
	// +optional
	metav1.ObjectMeta `json:"metadata,omitzero"`

	// spec defines the desired state of Memcached
	// +required
	Spec MemcachedSpec `json:"spec"`

	// status defines the observed state of Memcached
	// +optional
	Status MemcachedStatus `json:"status,omitzero"`
}

// +kubebuilder:object:root=true

// MemcachedList contains a list of Memcached
type MemcachedList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitzero"`
	Items           []Memcached `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(s *runtime.Scheme) error {
		s.AddKnownTypes(SchemeGroupVersion, &Memcached{}, &MemcachedList{})
		return nil
	})
}
