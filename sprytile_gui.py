# The gpu module based GUI runs on every backend (OpenGL, Vulkan, Metal),
# so there is no longer a separate Apple Silicon implementation
import spryTile_OS_EverythingElse


def register():
    spryTile_OS_EverythingElse.register()


def unregister():
    spryTile_OS_EverythingElse.unregister()


if __name__ == '__main__':
    register()
