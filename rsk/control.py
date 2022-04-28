from concurrent.futures import thread
import copy
from multiprocessing.dummy.connection import Client
import zmq
import time
import uuid
import threading
from . import robots, utils, client, field_dimensions, tasks

class Control:
    def __init__(self, robots):
        self.robots = robots

        # Publishing server
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.REP)
        self.socket.bind("tcp://*:7558")
        self.master_key = str(uuid.uuid4())

        # Target for client
        self.targets = {
            robot: None
            for robot in utils.all_robots()
        }
        self.targets_buffer = {}
        self.lock = threading.Lock()

        self.tasks = {}

        self.teams = {
            team: {
                "allow_control": True,
                "key": "",
                "packets": 0
            }
            for team in utils.robot_teams()
        }

    def add_task(self, task:tasks.ControlTask):
        self.lock.acquire()
        self.tasks[task.name] = task
        self.lock.release()

    def has_task(self, task_name:str):
        return task_name in self.tasks
    
    def remove_task(self, name:str):
        self.lock.acquire()
        if name in self.tasks:
            del self.tasks[name]
        self.lock.release()

    def thread(self):
        while self.running:
            self.socket.RCVTIMEO = 1000
            try:
                json = self.socket.recv_json()
                response = [False, 'Unknown error']

                if type(json) == list and len(json) == 4:
                    key, team, number, command = json

                    if team in self.teams:
                        allow_control = True

                        if key != self.master_key:
                            tasks = self.robot_tasks(team, number)
                            if self.teams[team]['key'] != key:
                                response[1] = f"Bad key for team {team}"
                                allow_control = False
                            elif not self.teams[team]['allow_control']:
                                response[1] = f"You are not allowed to control the robots of team {team}"
                                allow_control = False
                            elif len(tasks):
                                reasons = str(tasks)
                                response[1] = f"Robot {number} of team {team} is preempted: {reasons}"
                                allow_control = False
                        
                        if allow_control:
                            marker = "%s%d" % (team, number)
                            if marker in self.robots.robots_by_marker:
                                if type(command) == list:
                                    if command[0] == 'kick' and len(command) == 2:
                                        self.robots.robots_by_marker[marker].kick(
                                            float(command[1]))
                                        response = [True, 'ok']
                                    elif command[0] == 'control' and len(command) == 4:
                                        self.robots.robots_by_marker[marker].control(
                                            float(command[1]), float(command[2]), float(command[3]))
                                        response = [True, 'ok']
                                    else:
                                        response[1] = 'Unknown command'
                            else:
                                response[1] = 'Unknown robot'

                        self.teams[team]['packets'] += 1

                self.socket.send_json(response)
            except zmq.error.Again:
                pass

    def start(self):
        self.running = True
        control_thread = threading.Thread(target=lambda: self.thread())
        control_thread.start()

        client_thread = threading.Thread(target=lambda: self.client_thread())
        client_thread.start()

    def stop(self):
        self.running = False

    def robot_tasks(self, team:str, number:int) -> list:
        tasks = []
        for task in self.tasks:
            for task_team, task_number in task.robots():
                if (team, number) == (task_team, task_number):
                    tasks.append(task)

        return tasks

    def status(self):
        state = copy.deepcopy(self.teams)
        state[team]['preemption_reasons'] = {robot: [] for robot in utils.all_robots()}

        for task in self.tasks:
            for team, number in task.robots():
                state[team]['preemption_reasons'][number].append(task.name)

        return state

    def allowTeamControl(self, team:str, allow:bool):
        self.teams[team]['allow_control'] = allow

    def emergency(self):
        self._set_target_all(None)

        for team in utils.robot_teams():
            self.allowTeamControl(team, False)

        for port in self.robots.robots:
            self.robots.robots[port].control(0, 0, 0)

    def setKey(self, team, key):
        self.teams[team]['key'] = key

    def client_thread(self):
        self.client = client.Client(key=self.master_key)

        [_, field_DownRight_out, _, field_UpLeft_out] = field_dimensions.fieldCoordMargin(0.25)

        while self.running:
            # Keeping robots on sight
            for team, number in utils.all_robots():
                robot = self.client.robots[team][number]
                if robot.pose is not None:
                    intersect_field_in = not bool((field_UpLeft_out[0]<=robot.pose[0]<=field_DownRight_out[0]) 
                            and 
                            (field_DownRight_out[1]<=robot.pose[1]<=field_UpLeft_out[1]))
                    
                    task_name = 'out-of-game-%s' % utils.robot_list2str(team, number)

                    if intersect_field_in:
                        task = tasks.GoToTask(task_name, team, number, (0., 0, 0.))
                        self.add_task(task)
                        
                    else: 
                        if self.has_task(task_name):
                            task = tasks.StopTask(task_name, team, number, forever=False)
                            self.add_task(task)

            # Handling robot's goto, since client interaction access network, we can't afford to
            # lock a mutex during client calls, we store order in the temporary buffer list
            self.lock.acquire()
            tasks_to_tick = [task for task in self.tasks.values()]
            self.lock.release()

            # Sorting tasks by priority
            tasks_to_tick = sorted(tasks_to_tick, lambda task: -task.priority)
            robots_ticked = set()

            # Ticking all the tasks
            for task in tasks_to_tick:
                for team, number in task.robots():
                    if (team, number) not in robots_ticked:
                        # Robot was not ticked yet by an higher-priority task
                        robots_ticked.add((team, number))

                        try:
                            task.tick(self.client.robots[team][number])
                        except client.ClientError:
                            print("Error in control's client")

                    if task.finished(self.client):
                        to_delete = task.name

            # Removing finished tasks
            self.lock.acquire()
            for task_name in to_delete:
                del self.tasks[task_name]
            self.lock.relase()

            time.sleep(0.01)
