import pygame
from multiprocessing.connection import Connection
from collections import namedtuple

from essential.exception import ExceptionMessage
from . import gamecore, gameobject
from .. import communication as comm
from ..communication import SceneInfo, GameInstruction

class PingPong:
	"""
	The game core for the maching learning mode
	"""
	def __init__(self, \
		instruct_pipe_1P: Connection, scene_info_pipe_1P: Connection, \
		instruct_pipe_2P: Connection, scene_info_pipe_2P: Connection, \
		main_pipe: Connection):
		"""
		Constructor

		@param instruct_pipe_1P The pipe for receiving GameInstruction from 1P player
		@param scene_info_pipe_1P The pipe for sending SceneInfo to 1P player
		@param instruct_pipe_2P The pipe for receiving GameInstruction from 2P player
		@param scene_info_pipe_2P The pipe for sending SceneInfo to 2P player
		@param main_pipe The pipe for sending modified SceneInfo to the main process
		"""
		CommunicationPipe = namedtuple("CommunicationPipe", ("recv_end", "send_end"))
		self._ml_pipes = {
			"1P": CommunicationPipe(instruct_pipe_1P, scene_info_pipe_1P),
			"2P": CommunicationPipe(instruct_pipe_2P, scene_info_pipe_2P),
		}
		self._main_pipe = main_pipe

		self._init_pygame()

	def _init_pygame(self):
		self._clock = pygame.time.Clock()

	def game_loop(self, fps: int):
		"""
		The main loop of the game execution

		@param fps The updating rate of the game
		"""
		scene = gamecore.Scene(False)
		scene_info = scene.fill_scene_info_obj(SceneInfo())

		self._wait_ml_process_ready()

		# Set the first tick
		self._clock.tick_busy_loop(fps)

		while True:
			# Send the scene info to the ml processes and wait for instructions
			self._send_scene_info(scene_info)

			self._clock.tick_busy_loop(fps)

			instruction_1P = self._recv_instruction("1P")
			instruction_2P = self._recv_instruction("2P")
			self._main_pipe.send((scene_info, (instruction_1P, instruction_2P)))

			# Update the scene
			game_status = scene.update( \
				gamecore.PlatformMoveAction[instruction_1P.command.value], \
				gamecore.PlatformMoveAction[instruction_2P.command.value])
			scene_info = scene.fill_scene_info_obj(SceneInfo())

			# If either of side wins, reset the scene and wait for ml processes
			# getting ready for the next round
			if game_status == gamecore.GameStatus.GAME_1P_WIN or \
			   game_status == gamecore.GameStatus.GAME_2P_WIN:
				self._send_scene_info(scene_info)
				self._main_pipe.send((scene_info, None))

				scene.reset()
				scene_info = scene.fill_scene_info_obj(SceneInfo())
				# Wait for ml processes doing their reseting jobs
				self._wait_ml_process_ready()

	def _send_scene_info(self, scene_info: SceneInfo):
		"""
		Send the SceneInfo to both ml process

		@param scene_info The scene info to be sent
		"""
		self._ml_pipes["1P"].send_end.send(scene_info)
		self._ml_pipes["2P"].send_end.send(scene_info)

	def _recv_instruction(self, from_side: str) -> GameInstruction:
		"""
		Get the GameInstruction from the target pipe

		This function is non-blocking. If there is nothing available in the pipe,
		it will return a dummy GameInstruction.

		@param from_side Should be either "1P" or "2P".
		"""
		# Only "1P" or "2P" is the valid side.
		if from_side in ("1P", "2P"):
			target_pipe = self._ml_pipes[from_side].recv_end
		else:
			raise ValueError("Invalid from_side: {}. " \
				"Should be either \"1P\" or \"2P\".".format(from_side))

		if target_pipe.poll():
			instruction = target_pipe.recv()

			# Pass the exception to the main process
			if isinstance(instruction, ExceptionMessage):
				self._main_pipe.send((instruction, None))

			# Invalid instruction object
			if not isinstance(instruction, GameInstruction):
				return GameInstruction(-1, comm.PlatformAction.NONE)

			# Invalid PlatformAction instruction
			if instruction.command != comm.PlatformAction.MOVE_LEFT and \
			   instruction.command != comm.PlatformAction.MOVE_RIGHT:
				instruction.command = comm.PlatformAction.NONE
		else:
			return GameInstruction(-1, comm.PlatformAction.NONE)

		return instruction

	def _wait_ml_process_ready(self):
		"""
		Wait until receiving the ready command from both ml processes
		"""
		def wait_ready_command(target_pipe):
			while True:
				ready_instruct = target_pipe.recv()

				# Pass the exception to the main process and exit the waiting loop
				if isinstance(ready_instruct, ExceptionMessage):
					self._main_pipe.send((ready_instruct, None))
					return

				if isinstance(ready_instruct, GameInstruction) and \
				   ready_instruct.command == comm.PlatformAction.READY:
					return

		wait_ready_command(self._ml_pipes["1P"].recv_end)
		wait_ready_command(self._ml_pipes["2P"].recv_end)

class Screen:
	"""
	The drawing process for displaying the status of the game
	"""
	def __init__(self, scene_info_pipe: Connection, record_handler = lambda x: None):
		self._scene_info_pipe = scene_info_pipe
		self._record_handler = record_handler

		self._init_pygame()
		self._create_surface()

	def _init_pygame(self):
		pygame.display.init()
		self._screen = pygame.display.set_mode(gamecore.display_area_size)
		pygame.display.set_caption("PingPong")

		pygame.font.init()
		self._font = pygame.font.Font(None, 22)
		self._font_pos_1P = (1, 4)
		self._font_pos_2P = (1, gamecore.display_area_size[1] - 21)
		self._font_pos_speed = (gamecore.display_area_size[0] - 75, \
			gamecore.display_area_size[1] - 21)

	def _create_surface(self):
		ball_sprite = gameobject.Ball((0, 0))
		ball_sprite.create_surface()
		platform_1P_sprite = gameobject.Platform((0, 0), (0, 0))
		platform_1P_sprite.create_surface(gamecore.color_1P)
		platform_2P_sprite = gameobject.Platform((0, 0), (0, 0))
		platform_2P_sprite.create_surface(gamecore.color_2P)

		self._ball_surface = ball_sprite.image
		self._platform_1P_surface = platform_1P_sprite.image
		self._platform_2P_surface = platform_2P_sprite.image

	def _check_going(self):
		"""
		Check if the game window is closed or ESC key is pressed.
		"""
		for event in pygame.event.get():
			if event.type == pygame.QUIT or \
				(event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
				return False
		return True

	def draw_loop(self, game_over_score: int):
		"""
		Receive the SceneInfo from the game process and draw on the window

		It will count the score accroding to the SceneInfo received.
		If either of side reaches `game_over_score`, exit the loop.
		"""
		score = [0, 0]	# 1P, 2P

		while self._check_going():
			scene_info, instructions = self._scene_info_pipe.recv()
			# If receive an exception, pass the exception and quit the game.
			if isinstance(scene_info, ExceptionMessage):
				return scene_info

			self._record_handler(scene_info)

			# If either of side wins, print the game status and update the score
			if scene_info.status == gamecore.GameStatus.GAME_1P_WIN or \
			   scene_info.status == gamecore.GameStatus.GAME_2P_WIN:
				print("Frame: {}, Status: {}" \
					.format(scene_info.frame, scene_info.status))
				print("-----")

				# Update the score
				if scene_info.status == gamecore.GameStatus.GAME_1P_WIN:
					score[0] += 1
				else:
					score[1] += 1
				if score[0] == game_over_score or \
				   score[1] == game_over_score:
					break

			self._screen.fill((0, 0, 0))
			self._screen.blit(self._ball_surface, scene_info.ball)
			self._screen.blit(self._platform_1P_surface, scene_info.platform_1P)
			self._screen.blit(self._platform_2P_surface, scene_info.platform_2P)

			font_surface_1P = self._font.render( \
				"1P score: {}".format(score[0]), True, gamecore.color_1P)
			font_surface_2P = self._font.render( \
				"2P score: {}".format(score[1]), True, gamecore.color_2P)
			font_surface_speed = self._font.render( \
				"Speed: {}".format(scene_info.ball_speed), True, (255, 255, 255))
			self._screen.blit(font_surface_1P, self._font_pos_1P)
			self._screen.blit(font_surface_2P, self._font_pos_2P)
			self._screen.blit(font_surface_speed, self._font_pos_speed)

			pygame.display.flip()

		if score[0] > score[1]:
			print("1P wins!")
		else:
			print("2P wins!")
		print("Final score: {}-{}".format(score[0], score[1]))
