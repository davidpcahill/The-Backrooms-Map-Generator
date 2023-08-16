import pygame
import random

# Initialize pygame
pygame.init()

# Adjustable variables
SCREEN_WIDTH = 1920  # Width of the screen in pixels
SCREEN_HEIGHT = 1080  # Height of the screen in pixels
CELL_SIZE = 8  # Size of each cell in pixels
MAZE_FILL_PERCENTAGE = 0.8  # Desired maze fill percentage
NUM_MAZES = 1000  # Number of mazes to overlay
STOP_COLLISION_PROBABILITY = 0.5  # Probability of stopping if colliding with previous maze
NUM_ROOMS = 5  # Number of rooms to generate
ROOM_WIDTH_RANGE = (1, 32)  # Range of room width (min, max)
ROOM_HEIGHT_RANGE = (1, 32)  # Range of room height (min, max)
NUM_PILLAR_ROOMS = 5  # Number of rooms with pillars
PILLAR_ROOM_WIDTH_RANGE = (1, 32)  # Range of pillar room width (min, max)
PILLAR_ROOM_HEIGHT_RANGE = (1, 32)  # Range of pillar room height (min, max)
PILLAR_SPACING_RANGE = (2, 5)  # Range of pillar spacing (min, max)

# Calculate the number of cells in each dimension
NUM_COLS = SCREEN_WIDTH // CELL_SIZE
NUM_ROWS = SCREEN_HEIGHT // CELL_SIZE

# Colors
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

# Initialize the screen
screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
pygame.display.set_caption("Maze Generator")

# Function to draw the maze with paths of different widths
def draw_maze(maze):
    for row in maze:
        for cell in row:
            x, y, width, visited = cell
            if visited:
                pygame.draw.rect(screen, WHITE, (x, y, width, width))
            else:
                pygame.draw.rect(screen, BLACK, (x, y, width, width))

# Generate a maze using Prim's Algorithm
def generate_maze(width, height):
    maze = [[[x, y, CELL_SIZE, False] for x in range(0, width, CELL_SIZE)] for y in range(0, height, CELL_SIZE)]
    visited_cells = set()

    for _ in range(NUM_MAZES):
        x, y = random.randint(0, NUM_COLS - 1), random.randint(0, NUM_ROWS - 1)
        visited_cells.add((x, y))
        frontier = [(x, y)]

        while len(visited_cells) / (NUM_COLS * NUM_ROWS) < MAZE_FILL_PERCENTAGE:
            if not frontier:
                break

            x, y = random.choice(frontier)
            visited_cells.add((x, y))
            frontier.remove((x, y))

            maze[y][x][3] = True

            neighbors = []
            if x > 1 and (x - 2, y) not in visited_cells:
                neighbors.append((x - 2, y))
            if x < NUM_COLS - 2 and (x + 2, y) not in visited_cells:
                neighbors.append((x + 2, y))
            if y > 1 and (x, y - 2) not in visited_cells:
                neighbors.append((x, y - 2))
            if y < NUM_ROWS - 2 and (x, y + 2) not in visited_cells:
                neighbors.append((x, y + 2))

            if neighbors:
                next_cell = random.choice(neighbors)
                nx, ny = next_cell

                # Check if the next cell collides with a previous maze
                if random.random() > STOP_COLLISION_PROBABILITY or maze[(y + ny) // 2][(x + nx) // 2][3] == False:
                    frontier.append((nx, ny))
                    maze[(y + ny) // 2][(x + nx) // 2][3] = True

    return maze

# Generate rooms on the maze
def generate_rooms(maze):
    for _ in range(NUM_ROOMS):
        room_width = random.randint(ROOM_WIDTH_RANGE[0], ROOM_WIDTH_RANGE[1])
        room_height = random.randint(ROOM_HEIGHT_RANGE[0], ROOM_HEIGHT_RANGE[1])
        x = random.randint(0, NUM_COLS - room_width)
        y = random.randint(0, NUM_ROWS - room_height)

        for row in range(y, y + room_height):
            for col in range(x, x + room_width):
                maze[row][col][3] = True

# Generate rooms with pillars on the maze
def generate_pillar_rooms(maze):
    for _ in range(NUM_PILLAR_ROOMS):
        room_width = random.randint(PILLAR_ROOM_WIDTH_RANGE[0], PILLAR_ROOM_WIDTH_RANGE[1])
        room_height = random.randint(PILLAR_ROOM_HEIGHT_RANGE[0], PILLAR_ROOM_HEIGHT_RANGE[1])
        x = random.randint(0, NUM_COLS - room_width)
        y = random.randint(0, NUM_ROWS - room_height)

        for row in range(y, y + room_height):
            for col in range(x, x + room_width):
                maze[row][col][3] = True

        # Add pillars
        pillar_spacing = random.randint(PILLAR_SPACING_RANGE[0], PILLAR_SPACING_RANGE[1])
        for row in range(y, y + room_height, pillar_spacing):
            for col in range(x, x + room_width, pillar_spacing):
                maze[row][col][3] = False

# Generate the maze, rooms, and rooms with pillars
maze = generate_maze(SCREEN_WIDTH, SCREEN_HEIGHT)
generate_rooms(maze)
generate_pillar_rooms(maze)

# Main loop
running = True
while running:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        elif event.type == pygame.KEYDOWN and event.key == pygame.K_r:
            # Regenerate the maze, rooms, and rooms with pillars
            maze = generate_maze(SCREEN_WIDTH, SCREEN_HEIGHT)
            generate_rooms(maze)
            generate_pillar_rooms(maze)

    draw_maze(maze)
    pygame.display.flip()

pygame.quit()
