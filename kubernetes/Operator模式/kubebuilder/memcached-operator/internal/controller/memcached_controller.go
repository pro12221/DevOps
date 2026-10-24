/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"fmt"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	cachev1alpha1 "example.com/memcached-operator/api/v1alpha1"
)

const (
	// typeAvailableMemcached 是我们维护的 status condition 类型。
	typeAvailableMemcached = "Available"

	// memcachedImage 固定版本，避免 latest 漂移导致"昨天能跑今天不能跑"。
	memcachedImage = "memcached:1.6.26-alpine3.19"
)

// MemcachedReconciler reconciles a Memcached object
type MemcachedReconciler struct {
	client.Client                 // 内嵌（embedding），直接拥有 Get/Create/Update/Patch 等方法
	Scheme        *runtime.Scheme // 记录"哪些 Go 类型对应哪些 k8s API 资源"，序列化/反序列化要用

}

// RBAC 注释

// +kubebuilder:rbac:groups=cache.example.com,resources=memcacheds,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=cache.example.com,resources=memcacheds/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=cache.example.com,resources=memcacheds/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=core,resources=pods,verbs=get;list;watch
// +kubebuilder:rbac:groups=core,resources=services,verbs=get;list;watch;create;update;patch;delete

// Reconcile 是调谐循环的入口：把集群现状推向 Memcached CR 声明的期望状态。
// 必须幂等——同一份输入跑 1 次和跑 100 次，结果一致。
//
// For more details, check Reconcile and its Result here:
// - https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.25.0/pkg/reconcile
func (r *MemcachedReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	// 1. 取 CR。取不到且是 NotFound，说明对象已被删除，直接结束。
	memcached := &cachev1alpha1.Memcached{}
	if err := r.Get(ctx, req.NamespacedName, memcached); err != nil {
		if apierrors.IsNotFound(err) {
			log.Info("Memcached resource not found, ignoring since object must be deleted")
			return ctrl.Result{}, nil
		}
		log.Error(err, "Failed to get Memcached")
		return ctrl.Result{}, err
	}

	// 2. 第一次看到这个 CR 时先写一个 Unknown 条件，告诉外界"我接手了"。
	if len(memcached.Status.Conditions) == 0 {
		if err := r.setCondition(ctx, memcached, metav1.Condition{
			Type:               typeAvailableMemcached,
			Status:             metav1.ConditionUnknown,
			Reason:             "Reconciling",
			Message:            "Starting reconciliation",
			ObservedGeneration: memcached.Generation,
		}); err != nil {
			log.Error(err, "Failed to update Memcached status")
			return ctrl.Result{}, err
		}
	}

	// 3. 找到/创建 CR 对应的 Deployment（同名同 namespace）。
	createdDeployment := false
	found := &appsv1.Deployment{}
	err := r.Get(ctx, types.NamespacedName{Name: memcached.Name, Namespace: memcached.Namespace}, found) // Get 的第三个参数既决定查哪种资源，也决定把结果写进哪
	if err != nil && apierrors.IsNotFound(err) {                                                         // Deployment 不存在就创建一个。
		dep, err := r.deploymentForMemcached(memcached) // 只在内存里构造 Deployment 对象，不提交。
		if err != nil {
			log.Error(err, "Failed to define new Deployment for Memcached")
			_ = r.setCondition(ctx, memcached, metav1.Condition{ // 更新 CR 的 status，告诉外界"我尝试创建 Deployment 失败了"。
				Type:               typeAvailableMemcached,
				Status:             metav1.ConditionFalse,
				Reason:             "Reconciling",
				Message:            fmt.Sprintf("Failed to define Deployment for %s: %v", memcached.Name, err),
				ObservedGeneration: memcached.Generation,
			})
			return ctrl.Result{}, err
		}

		log.Info("Creating a new Deployment",
			"Deployment.Namespace", dep.Namespace, "Deployment.Name", dep.Name) // 记录日志，方便排查问题。
		if err := r.Create(ctx, dep); err != nil { // 提交到 k8s API server，创建 Deployment。
			log.Error(err, "Failed to create new Deployment",
				"Deployment.Namespace", dep.Namespace, "Deployment.Name", dep.Name)
			return ctrl.Result{}, err
		}

		// Deployment 刚创建，Pod 还没起来；1 分钟后再看一次（Owns 的 watch 通常会更快触发）。
		createdDeployment = true
	} else if err != nil {
		log.Error(err, "Failed to get Deployment")
		return ctrl.Result{}, err
	}

	// 4. 找到/创建 CR 对应的 Service（同名同 namespace）。
	svc := &corev1.Service{}
	svckeys := types.NamespacedName{Name: memcached.Name, Namespace: memcached.Namespace}
	if err := r.Get(ctx, svckeys, svc); err != nil {
		if !apierrors.IsNotFound(err) {
			log.Error(err, "Failed to get Service", "Service.Namespace", svckeys.Namespace, "Service.Name", svckeys.Name)
			return ctrl.Result{}, err
		}
		svc, err = r.serviceForMemcached(memcached)
		if err != nil {
			log.Error(err, "Failed to define new Service for Memcached")
			return ctrl.Result{}, err
		}
		if err := r.Create(ctx, svc); err != nil {
			log.Error(err, "Failed to create new Service",
				"Service.Namespace", svc.Namespace, "Service.Name", svc.Name)
			return ctrl.Result{}, err
		}
		log.Info("Created a new Service",
			"Service.Namespace", svc.Namespace, "Service.Name", svc.Name)
	}

	// Deployment 是这一轮刚建的，found 还是空对象，不能往下走副本同步。
	// 这个判断必须放在 Service 的 ensure 之外：Service 已存在时同样需要早退。
	if createdDeployment {
		// Pod 还没起来；1 分钟后再看一次（Owns 的 watch 通常会更快触发）。
		return ctrl.Result{RequeueAfter: 1 * time.Minute}, nil
	}

	// 5. 期望副本数 = spec.size；与 Deployment 上的实际值不一致就改回去。
	size := memcached.Spec.Size
	if found.Spec.Replicas == nil || *found.Spec.Replicas != size { //如果 Deployment 的副本数不等于 CR 期望值，就改回去。
		found.Spec.Replicas = ptr.To(size) // 只改副本数，别改别的字段，避免覆盖用户手动改的东西。
		if err := r.Update(ctx, found); err != nil {
			// 冲突说明别人刚改过，重新排队即可，不必当成错误刷日志。
			if apierrors.IsConflict(err) { // Deployment 被别人改了，重新排队。
				log.Info("Deployment was modified concurrently, retrying")
				return ctrl.Result{Requeue: true}, nil
			}
			log.Error(err, "Failed to update Deployment",
				"Deployment.Namespace", found.Namespace, "Deployment.Name", found.Name) // 如果改副本数失败，记录日志并更新 CR 的 status，告诉外界"我尝试改 Deployment 失败了"。
			_ = r.setCondition(ctx, memcached, metav1.Condition{ // 更新 CR 的 status，告诉外界"我尝试改 Deployment 失败了"。
				Type:               typeAvailableMemcached,
				Status:             metav1.ConditionFalse,
				Reason:             "Resizing",
				Message:            fmt.Sprintf("Failed to resize Deployment for %s: %v", memcached.Name, err),
				ObservedGeneration: memcached.Generation,
			})
			return ctrl.Result{}, err
		}
		// 改完副本数立即重新排队，尽快进入"检查是否就绪"的分支。
		return ctrl.Result{Requeue: true}, nil
	}

	// 6. 根据 Deployment 的就绪情况刷新 Available 条件。
	available := found.Status.ReadyReplicas == size && found.Status.ObservedGeneration >= found.Generation // 期望副本数=就绪副本数 并且 Deployment 已经观察到最新的 spec 变更，才算就绪。
	condition := metav1.Condition{
		Type:               typeAvailableMemcached,
		ObservedGeneration: memcached.Generation,
	}
	if available {
		condition.Status = metav1.ConditionTrue
		condition.Reason = "DeploymentReady"
		condition.Message = fmt.Sprintf("%d/%d memcached Pods ready", found.Status.ReadyReplicas, size)
	} else {
		condition.Status = metav1.ConditionFalse
		condition.Reason = "DeploymentNotReady"
		condition.Message = fmt.Sprintf("Waiting for Deployment: %d/%d Pods ready",
			found.Status.ReadyReplicas, size)
	}
	if err := r.setCondition(ctx, memcached, condition); err != nil {
		log.Error(err, "Failed to update Memcached status")
		return ctrl.Result{}, err
	}

	// 未就绪时 15 秒后再检查；就绪后不再主动轮询，交给 watch 事件驱动。
	if !available {
		return ctrl.Result{RequeueAfter: 15 * time.Second}, nil
	}
	return ctrl.Result{}, nil
}

// SetupWithManager 注册控制器：For(CR) 监听主资源，Owns(Deployment) 监听它拥有的从属资源。
// 这段是将 MemcachedReconciler 注册到控制器管理器中，使其能够监听 Memcached CR 和相关的 Deployment 资源的变化，从而触发 Reconcile 方法进行处理。
func (r *MemcachedReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&cachev1alpha1.Memcached{}). // 监听主资源 Memcached CR
		Owns(&appsv1.Deployment{}).      // 监听从属资源 Deployment
		Owns(&corev1.Service{}).         // 监听从属资源 Service
		Named("memcached").              // 给控制器起个名字，方便日志排查
		Complete(r)
}

// setCondition 只在条件真的变化时才写 status。
// 用 MergeFrom 生成 patch、走 /status 子资源：
//   - 不提交 spec，用户改的期望值不会被控制器覆盖；
//   - patch 不带 resourceVersion，天然规避 "object has been modified" 冲突。
func (r *MemcachedReconciler) setCondition(
	ctx context.Context, memcached *cachev1alpha1.Memcached, condition metav1.Condition) error {
	original := memcached.DeepCopy()                                       // 完整备份 CR，后续生成 patch 用。
	if !meta.SetStatusCondition(&memcached.Status.Conditions, condition) { // 条件没变就不写 status，避免无谓的 patch。
		return nil
	}
	return r.Status().Patch(ctx, memcached, client.MergeFrom(original))
}

func selectorForMemcached(m *cachev1alpha1.Memcached) map[string]string {
	return map[string]string{
		"app.kubernetes.io/name":     "memcached",
		"app.kubernetes.io/instance": m.Name,
	}
}

// func labelsForMemcached(m *cachev1alpha1.Memcached) map[string]string {
// 	l := selectorForMemcached(m)
// 	l["app.kubernetes.io/managed-by"] = "memcached-operator"
// 	return l
// }

func (r *MemcachedReconciler) serviceForMemcached(
	memcached *cachev1alpha1.Memcached) (*corev1.Service, error) {
	svc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:      memcached.Name,
			Namespace: memcached.Namespace,
		},
		Spec: corev1.ServiceSpec{
			Selector: selectorForMemcached(memcached),
			Ports: []corev1.ServicePort{{
				Name:       "memcached",
				Port:       11211,
				TargetPort: intstr.FromInt32(11211),
				Protocol:   corev1.ProtocolTCP,
			}},
		},
	}
	if err := ctrl.SetControllerReference(memcached, svc, r.Scheme); err != nil {
		return nil, err
	}
	return svc, nil
}

// deploymentForMemcached 根据 CR 构造期望的 Deployment（只在内存里构造，不提交）。
func (r *MemcachedReconciler) deploymentForMemcached(
	memcached *cachev1alpha1.Memcached) (*appsv1.Deployment, error) {
	replicas := memcached.Spec.Size // 拿到期望副本数
	//构造 Deployment 的标签，方便后续查询和管理

	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:      memcached.Name,
			Namespace: memcached.Namespace,
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: ptr.To(replicas),
			Selector: &metav1.LabelSelector{
				MatchLabels: selectorForMemcached(memcached),
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: selectorForMemcached(memcached)},
				Spec: corev1.PodSpec{
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot: ptr.To(true),
						SeccompProfile: &corev1.SeccompProfile{
							Type: corev1.SeccompProfileTypeRuntimeDefault,
						},
					},
					Containers: []corev1.Container{{
						Name:            "memcached",
						Image:           memcachedImage,
						ImagePullPolicy: corev1.PullIfNotPresent,
						SecurityContext: &corev1.SecurityContext{
							// 镜像里的用户叫 memcache（非数字 UID）。只写 runAsNonRoot 时
							// kubelet 无法验证"它到底是不是 root"，会报 CreateContainerConfigError；
							// 显式给出数字 UID（11211）才能通过校验。
							RunAsUser:                ptr.To(int64(11211)),
							AllowPrivilegeEscalation: ptr.To(false),
							Capabilities: &corev1.Capabilities{
								Drop: []corev1.Capability{"ALL"},
							},
						},
						Ports: []corev1.ContainerPort{{
							Name:          "memcached",
							ContainerPort: 11211,
							Protocol:      corev1.ProtocolTCP,
						}},
						Command: []string{"memcached", "--memory-limit=64", "-o", "modern", "-v"},
						ReadinessProbe: &corev1.Probe{
							ProbeHandler: corev1.ProbeHandler{
								TCPSocket: &corev1.TCPSocketAction{Port: intstr.FromInt32(11211)},
							},
							InitialDelaySeconds: 3,
							PeriodSeconds:       5,
						},
						Resources: corev1.ResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse("50m"),
								corev1.ResourceMemory: resource.MustParse("64Mi"),
							},
							Limits: corev1.ResourceList{
								corev1.ResourceMemory: resource.MustParse("128Mi"),
							},
						},
					}},
				},
			},
		},
	}

	// 关键一步：把 CR 设为 Deployment 的 controller owner。
	// 既让 Owns(Deployment) 能过滤出"我的"Deployment，也让删除 CR 时级联 GC。
	if err := ctrl.SetControllerReference(memcached, dep, r.Scheme); err != nil {
		return nil, err
	}
	return dep, nil
}
