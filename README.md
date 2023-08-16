# The-Backrooms-Map-Generator
This was written by ChatGPT in collaboration with me. It came out pretty amazingly well. It uses python and pygame.

Multiple adjustment variables are available in the script. I set to ones that have decent results.

# Adjustable variables

Note: Higher NUM_MAZES results in more white space (less maze-like, more backrooms-like). But everything must be balanced as this has diminishing returns. Try out the default settings in the script first and play from there. Feel free to contribute!

*Examples from one of the more sparse, larger-pixel screenshots*
```
SCREEN_WIDTH = 1920  # Width of the screen in pixels
SCREEN_HEIGHT = 1080  # Height of the screen in pixels
CELL_SIZE = 16  # Size of each cell in pixels
MAZE_FILL_PERCENTAGE = 0.8  # Desired maze fill percentage
NUM_MAZES = 600  # Number of mazes to overlay
STOP_COLLISION_PROBABILITY = 0.7  # Probability of stopping if colliding with previous maze
NUM_ROOMS = 2  # Number of rooms to generate
ROOM_WIDTH_RANGE = (1, 32)  # Range of room width (min, max)
ROOM_HEIGHT_RANGE = (1, 32)  # Range of room height (min, max)
NUM_PILLAR_ROOMS = 1  # Number of rooms with pillars
PILLAR_ROOM_WIDTH_RANGE = (1, 32)  # Range of pillar room width (min, max)
PILLAR_ROOM_HEIGHT_RANGE = (1, 32)  # Range of pillar room height (min, max)
PILLAR_SPACING_RANGE = (3, 5)  # Range of pillar spacing (min, max)
NUM_CUSTOM_ROOMS = 1  # Number of custom-shaped rooms to generate
MIN_NUM_SIDES = 2     # Minimum number of sides for a custom room
MAX_NUM_SIDES = 8     # Maximum number of sides for a custom room
MIN_CUSTOM_ROOM_RADIUS = 1   # Minimum radius of custom room
MAX_CUSTOM_ROOM_RADIUS = 16  # Maximum radius of custom room
```

## Controls
R = Regenerate Map

## Examples
![image](https://github.com/TagWolf/The-Backrooms-Map-Generator/assets/8665128/b3e26ceb-53e6-4504-836a-28563e1ccb75)

![image](https://github.com/TagWolf/The-Backrooms-Map-Generator/assets/8665128/61b7b2e8-a5e0-4c22-be12-ab98662fd0c8)

![image](https://github.com/TagWolf/The-Backrooms-Map-Generator/assets/8665128/144e2057-f79d-46ca-9c54-6521714f085b)

![image](https://github.com/TagWolf/The-Backrooms-Map-Generator/assets/8665128/f68452e2-79e1-4d88-b8b2-a4aa7ab0ae88)

![image](https://github.com/TagWolf/The-Backrooms-Map-Generator/assets/8665128/7f6e4e56-db4c-474f-b761-8728719a078c)

![image](https://github.com/TagWolf/The-Backrooms-Map-Generator/assets/8665128/ca530283-af62-4ad4-b3f8-35329f466e9f)

![image](https://github.com/TagWolf/The-Backrooms-Map-Generator/assets/8665128/2f429d06-5909-4507-a290-d4f8269fd6ee)

![image](https://github.com/TagWolf/The-Backrooms-Map-Generator/assets/8665128/70ba10f3-22f3-4fbe-917a-fadbda867f6d)
