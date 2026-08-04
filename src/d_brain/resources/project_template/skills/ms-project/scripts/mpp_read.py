#!/usr/bin/env python3
"""MPP Reader — читает Microsoft Project файлы (.mpp) через MPXJ.

Полная поддержка: задачи, ресурсы, назначения, WBS, иерархия, вехи.

Использование:
  mpp_read.py <file.mpp> [options]

Options:
  --format json|csv|md       Формат вывода (default: json)
  --fields <list>            Список полей через запятую (default: все)
  --filter-status <status>   Фильтр по статусу задачи
  --filter-date <YYYY-MM-DD> Задачи начинающиеся после этой даты
  --summary                  Только сводка без деталей задач
"""

import argparse
import csv
import io
import json
import os
import sys
from datetime import datetime

MPXJ_JAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mpxj.jar")
MPXJ_DEPS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "deps")
CLASSPATH_SEP = ";" if os.name == "nt" else ":"


def build_classpath():
    """Build full classpath: mpxj.jar + all dependency jars."""
    jars = [MPXJ_JAR]
    deps_dir = MPXJ_DEPS
    if os.path.isdir(deps_dir):
        for f in os.listdir(deps_dir):
            if f.endswith(".jar"):
                jars.append(os.path.join(deps_dir, f))
    return CLASSPATH_SEP.join(jars)


# Поля задачи по умолчанию
DEFAULT_FIELDS = [
    "id", "unique_id", "name", "duration", "start", "finish",
    "percent_complete", "priority", "resource_names", "resources",
    "predecessors", "notes", "milestone", "critical", "summary",
    "wbs", "outline_level", "outline_number"
]

FIELD_MAP = {
    "id": ("id", "int"),
    "unique_id": ("uniqueID", "int"),
    "name": ("name", "str"),
    "duration": ("duration", "str"),
    "start": ("start", "date"),
    "finish": ("finish", "date"),
    "percent_complete": ("percentageComplete", "float"),
    "priority": ("priority", "int"),
    "resource_names": ("resourceNames", "str"),
    "predecessors": ("predecessors", "str"),
    "notes": ("notes", "str"),
    "milestone": ("milestone", "bool"),
    "critical": ("critical", "bool"),
    "summary": ("summary", "bool"),
    "cost": ("cost", "float"),
    "baseline_start": ("baselineStart", "date"),
    "baseline_finish": ("baselineFinish", "date"),
    "actual_start": ("actualStart", "date"),
    "actual_finish": ("actualFinish", "date"),
    "wbs": ("wbs", "str"),
    "outline_level": ("outlineLevel", "int"),
    "outline_number": ("outlineNumber", "str"),
    "constraint_type": ("constraintType", "str"),
    "constraint_date": ("constraintDate", "date"),
}


def start_jvm():
    import jpype
    if not jpype.isJVMStarted():
        jpype.addClassPath(build_classpath())
        jvmpath = jpype.getDefaultJVMPath()
        jpype.startJVM(jvmpath, convertStrings=True)


def read_mpp(filepath):
    start_jvm()
    from jpype import JClass

    ProjectFile = JClass("net.sf.mpxj.ProjectFile")
    MPPReader = JClass("net.sf.mpxj.reader.UniversalProjectReader")

    project = MPPReader().read(filepath)
    return project


def format_date_value(value):
    """Форматирует Java дату в ISO строку."""
    if value is None:
        return None
    try:
        # Пробуем toInstant (для Instant)
        if hasattr(value, 'toInstant'):
            from jpype import JClass
            ZoneOffset = JClass("java.time.ZoneOffset")
            instant = value.toInstant(ZoneOffset.UTC)
            return str(instant)[:19]
        # Или просто toString
        return str(value)[:19]
    except Exception:
        return str(value)[:19]


def extract_task(task, fields=None):
    if fields is None:
        fields = DEFAULT_FIELDS

    result = {}
    for field in fields:
        if field not in FIELD_MAP:
            continue
        java_attr, dtype = FIELD_MAP[field]

        try:
            value = getattr(task, f"get{java_attr[0].upper()}{java_attr[1:]}")()
        except Exception:
            result[field] = None
            continue

        if value is None:
            result[field] = None
        elif dtype == "date":
            result[field] = format_date_value(value)
        elif dtype == "bool":
            result[field] = bool(value)
        elif dtype == "float":
            try:
                result[field] = round(float(value), 2)
            except (ValueError, TypeError):
                result[field] = str(value)
        elif dtype == "int":
            try:
                result[field] = int(value)
            except (ValueError, TypeError):
                result[field] = str(value)
        else:
            result[field] = str(value) if value else None

    return result


def extract_resource(resource):
    """Извлекает данные ресурса."""
    result = {
        "id": None,
        "unique_id": None,
        "name": None,
        "type": None,  # WORK, MATERIAL, COST
        "max_units": None,
        "standard_rate": None,
        "cost_per_use": None,
        "email": None,
        "group": None,
        "notes": None,
    }
    
    try:
        result["id"] = int(resource.getID()) if resource.getID() else None
    except:
        pass
    
    try:
        result["unique_id"] = int(resource.getUniqueID()) if resource.getUniqueID() else None
    except:
        pass
    
    try:
        result["name"] = str(resource.getName()) if resource.getName() else None
    except:
        pass
    
    try:
        rtype = resource.getType()
        if rtype:
            result["type"] = str(rtype)
    except:
        pass
    
    try:
        result["max_units"] = float(resource.getMaxUnits()) if resource.getMaxUnits() else None
    except:
        pass
    
    try:
        rate = resource.getStandardRate()
        if rate:
            result["standard_rate"] = float(rate.getAmount()) if hasattr(rate, 'getAmount') else str(rate)
    except:
        pass
    
    try:
        result["email"] = str(resource.getEmailAddress()) if resource.getEmailAddress() else None
    except:
        pass
    
    try:
        result["group"] = str(resource.getGroup()) if resource.getGroup() else None
    except:
        pass
    
    try:
        result["notes"] = str(resource.getNotes()) if resource.getNotes() else None
    except:
        pass
    
    return result


def extract_assignment(assignment):
    """Извлекает данные назначения (связь задача-ресурс)."""
    result = {
        "task_id": None,
        "task_unique_id": None,
        "task_name": None,
        "resource_id": None,
        "resource_unique_id": None,
        "resource_name": None,
        "units": None,
        "work": None,
        "actual_work": None,
        "remaining_work": None,
        "cost": None,
        "actual_cost": None,
    }
    
    try:
        task = assignment.getTask()
        if task:
            result["task_id"] = int(task.getID()) if task.getID() else None
            result["task_unique_id"] = int(task.getUniqueID()) if task.getUniqueID() else None
            result["task_name"] = str(task.getName()) if task.getName() else None
    except:
        pass
    
    try:
        resource = assignment.getResource()
        if resource:
            result["resource_id"] = int(resource.getID()) if resource.getID() else None
            result["resource_unique_id"] = int(resource.getUniqueID()) if resource.getUniqueID() else None
            result["resource_name"] = str(resource.getName()) if resource.getName() else None
    except:
        pass
    
    try:
        result["units"] = float(assignment.getUnits()) if assignment.getUnits() else None
    except:
        pass
    
    try:
        work = assignment.getWork()
        if work:
            result["work"] = str(work)
    except:
        pass
    
    try:
        actual = assignment.getActualWork()
        if actual:
            result["actual_work"] = str(actual)
    except:
        pass
    
    try:
        remaining = assignment.getRemainingWork()
        if remaining:
            result["remaining_work"] = str(remaining)
    except:
        pass
    
    try:
        result["cost"] = float(assignment.getCost()) if assignment.getCost() else None
    except:
        pass
    
    try:
        result["actual_cost"] = float(assignment.getActualCost()) if assignment.getActualCost() else None
    except:
        pass
    
    return result


def extract_resources_list(project):
    """Извлекает список всех ресурсов."""
    resources = []
    try:
        for resource in project.getResources():
            if resource:  # Пропускаем None (пустой ресурс с ID=0)
                res_data = extract_resource(resource)
                if res_data.get("name") or res_data.get("id") is not None:
                    resources.append(res_data)
    except Exception as e:
        print(f"Warning: error reading resources: {e}", file=sys.stderr)
    return resources


def extract_assignments_list(project):
    """Извлекает список всех назначений."""
    assignments = []
    try:
        for assignment in project.getResourceAssignments():
            if assignment:
                assign_data = extract_assignment(assignment)
                assignments.append(assign_data)
    except Exception as e:
        print(f"Warning: error reading assignments: {e}", file=sys.stderr)
    return assignments


def extract_task_resources(task):
    """Извлекает назначенные ресурсы для задачи."""
    resources = []
    try:
        for assignment in task.getResourceAssignments():
            if assignment:
                resource = assignment.getResource()
                if resource:
                    res_info = {
                        "id": int(resource.getID()) if resource.getID() else None,
                        "unique_id": int(resource.getUniqueID()) if resource.getUniqueID() else None,
                        "name": str(resource.getName()) if resource.getName() else None,
                        "units": float(assignment.getUnits()) if assignment.getUnits() else None,
                    }
                    resources.append(res_info)
    except:
        pass
    return resources


def extract_project_summary(project):
    summary = {
        "file_name": str(project.getProjectProperties().getName() or ""),
        "start_date": None,
        "finish_date": None,
        "total_tasks": 0,
        "milestones": 0,
        "critical_tasks": 0,
        "summary_tasks": 0,
        "resources_count": 0,
        "assignments_count": 0,
        "percent_complete": 0.0,
    }

    tasks = list(project.getTasks())
    summary["total_tasks"] = len(tasks)

    dates_start = []
    dates_finish = []
    completed = []

    for task in tasks:
        if task.getMilestone():
            summary["milestones"] += 1
        if task.getCritical():
            summary["critical_tasks"] += 1
        if task.getSummary():
            summary["summary_tasks"] += 1

        pct = task.getPercentageComplete()
        if pct is not None:
            completed.append(float(pct))

        start = task.getStart()
        finish = task.getFinish()
        if start is not None:
            dates_start.append(start)
        if finish is not None:
            dates_finish.append(finish)

    if dates_start:
        summary["start_date"] = format_date_value(min(dates_start))
    if dates_finish:
        summary["finish_date"] = format_date_value(max(dates_finish))
    if completed:
        summary["percent_complete"] = round(sum(completed) / len(completed), 1)

    summary["resources_count"] = len(list(project.getResources()))
    summary["assignments_count"] = len(list(project.getResourceAssignments()))

    return summary


def format_md(summary, tasks):
    lines = []
    lines.append(f"# {summary['file_name'] or 'Project Summary'}")
    lines.append("")
    lines.append(f"| Метрика | Значение |")
    lines.append(f"|---------|----------|")
    lines.append(f"| Задач | {summary['total_tasks']} |")
    lines.append(f"| Вех (milestones) | {summary['milestones']} |")
    lines.append(f"| Сводных (summary) | {summary.get('summary_tasks', 0)} |")
    lines.append(f"| Критических | {summary['critical_tasks']} |")
    lines.append(f"| Ресурсов | {summary['resources_count']} |")
    lines.append(f"| Назначений | {summary.get('assignments_count', 0)} |")
    lines.append(f"| Готовность | {summary['percent_complete']}% |")
    if summary["start_date"]:
        lines.append(f"| Старт | {summary['start_date']} |")
    if summary["finish_date"]:
        lines.append(f"| Финиш | {summary['finish_date']} |")
    lines.append("")

    if tasks:
        lines.append("## Задачи")
        lines.append("")
        header = "| ID | WBS | Название | Длительность | Старт | Финиш | % | Тип |"
        sep = "|-----|-----|----------|-------------|-------|-------|---|-----|"
        lines.append(header)
        lines.append(sep)
        for t in tasks:
            name = (t.get("name") or "")[:35]
            wbs = (t.get("wbs") or "")[:10]
            dur = t.get("duration") or ""
            start = (t.get("start") or "")[:10]
            finish = (t.get("finish") or "")[:10]
            pct = t.get("percent_complete") or 0
            
            # Определяем тип задачи
            task_type = ""
            if t.get("milestone"):
                task_type = "🚩"
            elif t.get("summary"):
                task_type = "📁"
            else:
                task_type = "📄"
            
            lines.append(f"| {t.get('id', '')} | {wbs} | {name} | {dur} | {start} | {finish} | {pct}% | {task_type} |")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="MPP Reader — Microsoft Project file parser")
    parser.add_argument("file", help="Path to .mpp file")
    parser.add_argument("--format", choices=["json", "csv", "md"], default="json")
    parser.add_argument("--fields", help="Comma-separated fields")
    parser.add_argument("--summary", action="store_true", help="Summary only")
    parser.add_argument("--filter-status", help="Filter by status")
    parser.add_argument("--filter-date", help="Tasks starting after YYYY-MM-DD")
    parser.add_argument("--full", action="store_true", help="Full output with resources and assignments")

    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    fields = None
    if args.fields:
        fields = [f.strip() for f in args.fields.split(",")]

    project = read_mpp(args.file)
    summary = extract_project_summary(project)

    if args.summary:
        if args.format == "json":
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(format_md(summary, []))
        return

    tasks = []
    for task in project.getTasks():
        t = extract_task(task, fields)
        # Добавляем назначенные ресурсы для задачи
        if args.full:
            t["resources"] = extract_task_resources(task)
        tasks.append(t)

    if args.filter_date:
        try:
            cutoff = args.filter_date
            tasks = [t for t in tasks if t.get("start") and t["start"] >= cutoff]
        except Exception:
            pass

    output = {"summary": summary, "tasks": tasks}
    
    if args.full:
        output["resources"] = extract_resources_list(project)
        output["assignments"] = extract_assignments_list(project)

    if args.format == "json":
        print(json.dumps(output, ensure_ascii=False, indent=2))
    elif args.format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=fields or DEFAULT_FIELDS)
        writer.writeheader()
        for t in tasks:
            writer.writerow(t)
    elif args.format == "md":
        print(format_md(summary, tasks))


if __name__ == "__main__":
    main()
