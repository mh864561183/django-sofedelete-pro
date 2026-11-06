# django-safedelete-pro

**`django-safedelete-pro`** 是基于 `django-safedelete` 深度优化的高性能 Django 软删除（逻辑删除）增强库。针对生产环境中**大批量级联删除慢、数据库联合唯一索引冲突、雪花算法/UUID 主键兼容、IDE 代码补全缺失**等痛点进行了全面升级。

---

## 核心特性与优化亮点

1. **按表批量级联软删除与恢复（$O(\text{Models})$ 级别 SQL）**
   - 原版在执行 `Model.objects.all().delete()` 或级联软删除时，采用 Python `for` 循环逐行 `save()`，1,000 条 A 关联 10,000 条 B、100,000 条 C 时会触发 **11.5 万+ 条 SQL**。
   - **`django-safedelete-pro`** 采用单次关联图收集 + `Cast(F('pk'))` 按表批量 `UPDATE`，相同场景仅需 **按表级别的数条 SQL** 即可完成。
2. **显式声明 3 个软删除核心字段（完美支持 IDE 智能提示与中文注释）**
   - `deleted_at`：删除时间（未删除为 `None`）
   - `is_deleted`：删除状态标识（`CharField(max_length=36)`，未删除默认为 `'-1'`，软删除后自动写入主键字符串 `str(pk)`，**同时兼容自增 ID、64 位雪花算法 ID、36 位 UUID**，完美解决 MySQL 等数据库中 `NULL` 值导致联合唯一索引失效的问题）
   - `deleted_by_cascade`：是否因父表级联而被软删除（恢复父表时仅恢复级联删除的子记录，不会误恢复此前已单独删除的子记录）
3. **修复多层关联（A $\rightarrow$ B $\rightarrow$ C）下 `undelete()` 指数级重复恢复 Bug**
4. **兼容最新 Django 3.2 / 4.x / 5.x / 6.x 与 Python 3.8 ~ 3.14**

---

## 一、 安装与配置

### 1. 安装
```bash
pip install django-safedelete-pro
```

### 2. 注册应用
在 Django 项目的 `settings.py` 中加入 `safedelete_pro`：
```python
INSTALLED_APPS = [
    # ...
    "safedelete_pro",
]
```

### 3. 可选全局配置（`settings.py`）
所有内置字段名均支持在 `settings.py` 中自定义（不配置则使用以下默认值）：
```python
SAFE_DELETE_FIELD_NAME = "deleted_at"                 # 默认: 'deleted_at'
SAFE_IS_DELETE_FIELD_NAME = "is_deleted"              # 默认: 'is_deleted'
SAFE_DELETE_CASCADED_FIELD_NAME = "deleted_by_cascade" # 默认: 'deleted_by_cascade'
# update_or_create 恢复软删除对象时是否将 created 返回为 True（默认 False）
SAFE_DELETE_INTERPRET_UNDELETED_OBJECTS_AS_CREATED = False
```

---

## 二、 快速上手

### 1. 定义模型与联合唯一索引
只需继承 `SafeDeleteModel`，模型会自动具备以下 3 个字段（无需手动编写，源码已显式声明以支持 PyCharm/VSCode 补全）：
```python
# SafeDeleteModel 内置字段定义：
deleted_at = models.DateTimeField(editable=False, null=True, db_index=True, verbose_name='删除时间')
is_deleted = models.CharField(editable=False, default='-1', max_length=36, db_index=True, verbose_name='删除状态')
deleted_by_cascade = models.BooleanField(editable=False, default=False, db_default=False, verbose_name='是否被级联删除')
```

业务模型示例（支持自增主键、雪花 ID、UUID）：
```python
import uuid
from django.db import models
from safedelete_pro.models import SafeDeleteModel
from safedelete_pro.config import SOFT_DELETE_CASCADE, SOFT_DELETE

class Department(SafeDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=64, verbose_name="部门名称")

    # 默认策略即为 SOFT_DELETE_CASCADE（级联软删除）
    _safedelete_policy = SOFT_DELETE_CASCADE

    class Meta:
        # 配合 is_deleted 实现“未删除记录 name 唯一，且允许存在多条同名已软删除历史记录”
        constraints = [
            models.UniqueConstraint(
                fields=["name", "is_deleted"],
                name="uniq_department_name_active",
            )
        ]

class Employee(SafeDeleteModel):
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="employees")
    username = models.CharField(max_length=64)
```

---

## 三、 删除策略与日常操作

### 1. 五种删除策略 (`_safedelete_policy`)
可在模型类属性 `_safedelete_policy` 设置，也可在调用 `.delete(force_policy=...)` 时临时指定：

| 策略常量 | 说明 |
| :--- | :--- |
| `SOFT_DELETE_CASCADE` | **（默认）级联软删除**：自动按表批量软删除所有 `on_delete=CASCADE` 的子表记录，并标记 `deleted_by_cascade=True`；同时处理 `PROTECT` 检查与 `SET_NULL`/`SET_DEFAULT`。 |
| `SOFT_DELETE` | **单表软删除**：仅软删除当前对象。 |
| `HARD_DELETE` | **物理删除**：直接从数据库 `DELETE` 删除。 |
| `HARD_DELETE_NOCASCADE` | **无关联时物理删除**：若无关联子对象则物理删除，若存在关联子对象则自动退化为软删除。 |
| `NO_DELETE` | **禁止删除**：调用 `delete()` 不执行任何删除。 |

### 2. 删除与恢复（单条 & 批量）
```python
from safedelete_pro import HARD_DELETE, SOFT_DELETE

# 1. 单条级联软删除（内部按表批量更新子表）
dept.delete()

# 2. QuerySet 批量级联软删除（高性能批量 SQL，不再逐条循环）
Department.objects.filter(name__startswith="测试").delete()

# 3. 强制物理删除
dept.delete(force_policy=HARD_DELETE)
Department.deleted_objects.all().delete(force_policy=HARD_DELETE)

# 4. 单条或批量恢复（自动级联恢复 deleted_by_cascade=True 的子记录，并将 is_deleted 重置为 '-1'）
dept.undelete()
Department.deleted_objects.filter(name__startswith="测试").undelete()
```

### 3. 三种内置查询管理器 (Managers)
- `Model.objects`：默认只查询未删除记录（`deleted_at__isnull=True`）。
  - 关联查询（如 `dept.employees`）可通过 `dept.employees.all_with_deleted()` 查询全部，或 `dept.employees.deleted_only()` 仅查已删除。
- `Model.all_objects`：查询全部记录（包含未删除与已软删除）。
- `Model.deleted_objects`：仅查询已软删除记录（`deleted_at__isnull=False`）。

---

## 四、 信号 (Signals) 与 Django Admin 集成

### 1. 信号监听
提供 `pre_softdelete`、`post_softdelete`、`post_undelete` 三个信号：
```python
from django.dispatch import receiver
from safedelete_pro.signals import pre_softdelete, post_softdelete, post_undelete

@receiver(post_softdelete, sender=Department)
def on_department_soft_deleted(sender, instance, using, **kwargs):
    print(f"部门已软删除: {instance.pk}, is_deleted={instance.is_deleted}")
```

### 2. Django Admin 后台集成
```python
from django.contrib import admin
from safedelete_pro.admin import SafeDeleteAdmin, SafeDeleteAdminFilter, highlight_deleted
from .models import Department

@admin.register(Department)
class DepartmentAdmin(SafeDeleteAdmin):
    list_display = (highlight_deleted, "name") + SafeDeleteAdmin.list_display
    list_filter = (SafeDeleteAdminFilter,) + SafeDeleteAdmin.list_filter
```
内置支持在 Admin 列表中标红显示已删除数据、按删除状态筛选、勾选批量恢复（Undelete）与批量彻底物理删除（Hard delete）。
