#!/usr/bin/env python3
"""
为现有项目生成缩略图的脚本
"""

import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from backend.core.database import SessionLocal
from backend.models.project import Project
from backend.utils.thumbnail_generator import generate_project_thumbnail

def generate_thumbnails_for_projects():
    """为所有没有缩略图的项目生成缩略图"""
    db = SessionLocal()
    try:
        # 查找所有没有缩略图但有视频文件的项目
        projects = db.query(Project).filter(
            Project.thumbnail.is_(None),
            Project.video_path.isnot(None)
        ).all()
        
        if not projects:
            print("✅ 所有项目都已有缩略图")
            return True
        
        print(f"📋 找到 {len(projects)} 个需要生成缩略图的项目")
        
        success_count = 0
        for project in projects:
            try:
                print(f"🎬 正在为项目 '{project.name}' ({project.id}) 生成缩略图...")
                
                # 检查视频文件是否存在
                video_path = Path(project.video_path)
                if not video_path.exists():
                    print(f"⚠️  视频文件不存在: {video_path}")
                    continue
                
                # 生成缩略图
                thumbnail_data = generate_project_thumbnail(project.id, video_path)
                
                if thumbnail_data:
                    # 保存到数据库
                    project.thumbnail = thumbnail_data
                    db.commit()
                    print(f"✅ 项目 '{project.name}' 缩略图生成成功")
                    success_count += 1
                else:
                    print(f"❌ 项目 '{project.name}' 缩略图生成失败")
                    
            except Exception as e:
                print(f"❌ 项目 '{project.name}' 处理失败: {e}")
                db.rollback()
                continue
        
        print(f"🎉 完成！成功为 {success_count}/{len(projects)} 个项目生成缩略图")
        return True
        
    except Exception as e:
        print(f"❌ 生成缩略图过程中发生错误: {e}")
        db.rollback()
        return False
    finally:
        db.close()

def generate_thumbnail_for_project(project_id: str):
    """为指定项目生成缩略图"""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        
        if not project:
            print(f"❌ 项目 {project_id} 不存在")
            return False
        
        if not project.video_path:
            print(f"❌ 项目 {project_id} 没有视频文件")
            return False
        
        # 检查视频文件是否存在
        video_path = Path(project.video_path)
        if not video_path.exists():
            print(f"❌ 视频文件不存在: {video_path}")
            return False
        
        print(f"🎬 正在为项目 '{project.name}' ({project.id}) 生成缩略图...")
        
        # 生成缩略图
        thumbnail_data = generate_project_thumbnail(project.id, video_path)
        
        if thumbnail_data:
            # 保存到数据库
            project.thumbnail = thumbnail_data
            db.commit()
            print(f"✅ 项目 '{project.name}' 缩略图生成成功")
            return True
        else:
            print(f"❌ 项目 '{project.name}' 缩略图生成失败")
            return False
            
    except Exception as e:
        print(f"❌ 处理项目 {project_id} 时发生错误: {e}")
        db.rollback()
        return False
    finally:
        db.close()

def main():
    """主函数"""
    if len(sys.argv) > 1:
        # 为指定项目生成缩略图
        project_id = sys.argv[1]
        print(f"🚀 开始为项目 {project_id} 生成缩略图...")
        if generate_thumbnail_for_project(project_id):
            print("🎉 缩略图生成完成！")
        else:
            print("❌ 缩略图生成失败")
            sys.exit(1)
    else:
        # 为所有项目生成缩略图
        print("🚀 开始为所有项目生成缩略图...")
        if generate_thumbnails_for_projects():
            print("🎉 所有缩略图生成完成！")
        else:
            print("❌ 缩略图生成失败")
            sys.exit(1)

if __name__ == "__main__":
    main()
