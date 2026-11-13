#!/usr/bin/env python3
"""
数据库初始化脚本
创建数据库表并插入初始数据
"""

import sys
from pathlib import Path

# 添加backend目录到Python路径
backend_dir = Path(__file__).parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from ..core.database import init_database, get_database_url
from ..core.config import init_paths, get_data_directory
from ..models.project import Project, ProjectStatus, ProjectType
from ..models.clip import Clip
from ..models.collection import Collection
from ..models.task import Task, TaskStatus, TaskType
from ..core.database import SessionLocal

def create_initial_data():
    """创建初始测试数据"""
    db = SessionLocal()
    try:
        # 检查是否已有数据
        existing_projects = db.query(Project).count()
        if existing_projects > 0:
            print("数据库中已有数据，跳过初始数据创建")
            return
        
        # 创建测试项目
        test_project = Project(
            name="测试项目",
            description="这是一个测试项目，用于验证系统功能",
            project_type=ProjectType.KNOWLEDGE,
            status=ProjectStatus.PENDING,
            processing_config={
                "chunk_size": 5000,
                "min_score_threshold": 0.7,
                "max_clips_per_collection": 5
            }
        )
        db.add(test_project)
        db.commit()
        db.refresh(test_project)
        
        # 创建测试任务
        test_task = Task(
            name="测试任务",
            description="测试处理任务",
            task_type=TaskType.VIDEO_PROCESSING,
            project_id=test_project.id,
            status=TaskStatus.PENDING,
            progress=0,
            current_step="等待开始",
            total_steps=6
        )
        db.add(test_task)
        
        # 创建测试切片
        test_clip = Clip(
            title="测试切片",
            content="这是一个测试切片的内容",
            start_time=0,
            end_time=30,
            score=0.8,
            project_id=test_project.id
        )
        db.add(test_clip)
        
        # 创建测试合集
        test_collection = Collection(
            title="测试合集",
            description="这是一个测试合集",
            project_id=test_project.id
        )
        db.add(test_collection)
        
        db.commit()
        print("✅ 初始测试数据创建成功")
        
    except Exception as e:
        print(f"❌ 创建初始数据失败: {e}")
        db.rollback()
    finally:
        db.close()

def main():
    """主函数"""
    print("🚀 开始初始化数据库...")
    
    # 初始化路径配置
    init_paths()
    
    # 显示数据库配置
    print(f"数据库URL: {get_database_url()}")
    print(f"数据目录: {get_data_directory()}")
    
    # 初始化数据库
    if init_database():
        print("✅ 数据库初始化成功")
        
        # 创建初始数据
        create_initial_data()
        
        print("🎉 数据库初始化完成！")
    else:
        print("❌ 数据库初始化失败")
        sys.exit(1)

if __name__ == "__main__":
    main() 