![header](https://capsule-render.vercel.app/api?type=waving&height=170&color=gradient&text=SDI%20CI/CV/CD%20Pipeline%20Framework&textBg=false&fontSize=40&fontColor=000000&descAlignY=50&fontAlignY=30)


**Table of contents** 
- [1. Introduction](#1-introduction)
- [2. Design](#2-design)
- [3. Scenario](#3-scenario)
- [4. Manual](#4-manual)


## 1. Introduction
This is a CI/CV/CD pipeline framework for software-defined future mobility.

Main features of this pipeline framework is:
* **Continuous Integration (CI)**: Automated service integration of AI-enabled mobility service (e.g., ROS2, Autoware)
![CI Flow](assets/CI_main.png)
* **Continuous Validation (CV)**: Simulation(e.g., Gazebo, CARLA)-based virtual validation of autonomous driving software
![Gazebo Simulation](assets/gazebo_office_world.png) 
* **Continuous Deployment (CD)**: Split deployment of moiblity software to mobility device and infrastrucutre (e.g., edge or cloud servers) 

For more details please check the APSEC'25 Tools Paper - [OrchestML](/CI/APSEC_2025_Tools.pdf).

## 2. Design

[Software Requirement Specification](https://docs.google.com/spreadsheets/d/1P-EfpCEkrHRfhBJHL3unYKW5okFbLe2h5jsJ6gXnRrw/edit?usp=sharing)

[Software Designs (Models)](https://drive.google.com/drive/folders/1rNpvV7xWhPPySddRkV-D2rOdhiFWtSDM?usp=drive_link)
- Component diagram
- Sequence diagram

## 3. Scenario
![Year 2 Scenario](assets/year2-scenario.png)


## 4. Manual
For more details regarding the usage of the CI tool, please visit the original repo of OrchestML [here](https://github.com/gurkhaman/OrchestML).