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
3. **修复多层关联（A $\rightarrow$ B $\rightarrow$ C）下 `undelete()` 指数级重复恢复 Bug，并支持唯一键冲突下自动恢复最近删除记录**
4. **原生面向 Django 5.0+ / 6.x（Python 3.10+），全面支持异步 ORM（`adelete` / `aundelete` / `asave` / `aupdate_or_create`）**

---

## 一、 安装与配置

### 1. 安装
```bash
pip install django-safedelete-pro
```

### 2. 注册应用
在 Django 项目的 `settings.py` 中加入 `django_safedelete_pro`：
```python
INSTALLED_APPS = [
    # ...
    "django_safedelete_pro",
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
from django_safedelete_pro.models import SafeDeleteModel
from django_safedelete_pro.config import SOFT_DELETE_CASCADE, SOFT_DELETE

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

### 1. 五种删除策略 (`_safedelete_policy`) 与多表关联规则
可在模型类属性 `_safedelete_policy` 设置，也可在调用 `.delete(force_policy=...)` 时临时指定：

| 策略常量 | 当前对象行为 | 对关联子表（`on_delete=CASCADE`）的行为 |
| :--- | :--- | :--- |
| `SOFT_DELETE_CASCADE` | **（默认）软删除** | 自动按表批量**软删除**所有继承自 `SafeDeleteModel` 的子表记录（标记 `deleted_by_cascade=True`）；若子表是普通 `models.Model` 则保留不删（防止父表恢复后子表数据丢失）；同时正常触发 `PROTECT` 检查与 `SET_NULL`/`SET_DEFAULT`。 |
| `SOFT_DELETE` | **单表软删除** | 仅软删除当前对象，**不触发**任何子表级联操作。 |
| `HARD_DELETE` | **物理删除** | 调用 Django 原生 `DELETE`，当前对象及所有 `on_delete=CASCADE` 的关联子表（**无论子表是 `SafeDeleteModel` 还是普通 `models.Model`**）都会被**直接物理删除**。 |
| `HARD_DELETE_NOCASCADE` | **条件物理删除** | 若无任何关联子对象则直接**物理删除**；若存在关联子对象则自动退化为 `SOFT_DELETE`（软删除）。 |
| `NO_DELETE` | **禁止删除** | 调用 `delete()` 不执行任何删除操作。 |

#### 核心说明：`_safedelete_policy` 影响哪个表？多层关联（A $\leftarrow$ B $\leftarrow$ C）如何配置？
- **作用边界**：`_safedelete_policy` 定义在**哪个模型上，就决定调用“该模型自身”的 `.delete()` 时采取什么动作**；而子表如何响应，则由**被删父表的 `_safedelete_policy`、子表外键的 `on_delete` 以及子表是否继承 `SafeDeleteModel` 共同决定**。
  - 在上方 `Department` $\leftarrow$ `Employee` 示例中，`Department` 配置 `_safedelete_policy = SOFT_DELETE_CASCADE` 决定的是执行 **`dept.delete()`** 时，不仅软删除 `Department` 自身，还会顺着 `Employee.department` 的 `on_delete=models.CASCADE` 把关联的 `Employee` 一并级联软删除。
  - 若单独执行 **`emp.delete()`**，则按 `Employee._safedelete_policy` 执行。

- **如果子表 C 继承的是 Django 默认的 `models.Model`（而非 `SafeDeleteModel`）会怎样？**
  - **当父表 B 执行软删除（`SOFT_DELETE_CASCADE`）时**：
    - 若 `C.b` 为 `on_delete=models.SET_NULL` / `SET_DEFAULT` / `PROTECT`：**依然完全生效**（会自动置空 `C.b_id` 或抛出 `ProtectedError`）。
    - 若 `C.b` 为 `on_delete=models.CASCADE`：**C 不会被删除（既不软删也不硬删）**。因为父表 B 只是执行了 `UPDATE` 软删除（行记录仍在数据库中），而普通 `models.Model` 没有 `deleted_at` 字段；如果此时把 C 物理删掉，一旦后续调用 `b.undelete()` 恢复 B，C 的数据就永久丢失了。
  - **当父表 B 执行物理删除（`HARD_DELETE`）时**：
    - 走 Django 原生级联删除，C 会随 B 一起被**直接物理删除**。

- **三层关联（B 关联 A，C 关联 B）典型配置方案**：
  假设需求为 **“删除 A 时级联软删除 B，而删除 B 时对 C 有不同处理”**：
  1. **希望 B 软删除时，C 的外键设为 `NULL`**：
     - `A`、`B` 继承 `SafeDeleteModel`（默认 `SOFT_DELETE_CASCADE`），`B.a` 设为 `on_delete=models.CASCADE`。
     - `C`（无论是否继承 `SafeDeleteModel`）的 `C.b` 外键设为 `on_delete=models.SET_NULL, null=True`。无论是删 A 级联到 B，还是直接删 B，C 的 `b_id` 都会自动批量置为 `NULL`，且 C 不会被删除。
  2. **希望 B 软删除时，C 保持原样不变（既不软删除 C，也不清空外键）**：
     - 方式 A：若 C 继承 `SafeDeleteModel`，将 `C.b` 外键设为 `on_delete=models.DO_NOTHING, db_constraint=False`（若仅在直接删 B 时不级联 C，也可将 `B._safedelete_policy = SOFT_DELETE`）。
     - 方式 B：若 C 本身不需要软删除能力，直接让 C 继承 Django 原生 `models.Model` 且 `on_delete=models.CASCADE`，在 B 软删除时 C 也会保持不变，而在 B 物理删除（`HARD_DELETE`）时 C 会自动级联物理删除。
  3. **希望 B 软删除时，直接物理删除（Hard Delete）C**：
     - **注意**：如上所述，仅让 C 继承 `models.Model` 在 B **软删除**时**不会**触发 C 的物理删除。若业务确实需要在 B 软删除的同时物理抹除 C，请监听 `B` 的 `post_softdelete` 信号（或重写 `B.delete()`）显式执行 `C.objects.filter(b=instance).delete(force_policy=HARD_DELETE)`（若 C 为普通 `models.Model` 则直接 `.delete()`）。

### 2. 删除与恢复（单条 & 批量）
```python
from django_safedelete_pro import HARD_DELETE, SOFT_DELETE

# 1. 单条级联软删除（内部按表批量更新子表）
dept.delete()

# 2. QuerySet 批量级联软删除（高性能批量 SQL，不再逐条循环）
Department.objects.filter(name__startswith="测试").delete()

# 3. 强制物理删除
dept.delete(force_policy=HARD_DELETE)
Department.deleted_objects.all().delete(force_policy=HARD_DELETE)

# 4. 单条或批量恢复（自动级联恢复 deleted_by_cascade=True 的子记录，并将 is_deleted 重置为 '-1'）
# 注：若模型配置了唯一约束（如 [name, is_deleted]）且历史中存在多条同名已软删除记录，
# 批量 undelete() 或 update_or_create() 会自动按 deleted_at 倒序仅恢复【最近一次删除】的记录，避免唯一键冲突报错。
dept.undelete()
Department.deleted_objects.filter(name="研发部").undelete()
```

### 3. Django 5.0+ 异步 ORM 操作
全面支持模型实例、QuerySet 与 Manager 的异步软删除、恢复与更新创建：
```python
# 1. 异步创建或唤醒最近一次软删除的同名记录
dept, created = await Department.objects.aupdate_or_create(name="研发部")

# 2. 单条实例异步软删除、恢复与保存
await dept.adelete()
await dept.aundelete()
await dept.asave()

# 3. QuerySet 批量异步级联软删除与恢复
await Department.objects.filter(name__startswith="测试").adelete()
await Department.deleted_objects.filter(name__startswith="测试").aundelete()
```

### 4. 三种内置查询管理器 (Managers)
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
from django_safedelete_pro.signals import pre_softdelete, post_softdelete, post_undelete

@receiver(post_softdelete, sender=Department)
def on_department_soft_deleted(sender, instance, using, **kwargs):
    print(f"部门已软删除: {instance.pk}, is_deleted={instance.is_deleted}")
```

### 2. Django Admin 后台集成
```python
from django.contrib import admin
from django_safedelete_pro.admin import SafeDeleteAdmin, SafeDeleteAdminFilter, highlight_deleted
from .models import Department

@admin.register(Department)
class DepartmentAdmin(SafeDeleteAdmin):
    list_display = (highlight_deleted, "name") + SafeDeleteAdmin.list_display
    list_filter = (SafeDeleteAdminFilter,) + SafeDeleteAdmin.list_filter
```
内置支持在 Admin 列表中标红显示已删除数据、按删除状态筛选、勾选批量恢复（Undelete）与批量彻底物理删除（Hard delete）。
